"""Tokens de sede del Worker (PLAN-V2 §1.7, CONTRATO §15.5).

    python -m tools.release site-token add --client covert --site S0042 [--out secrets.json]
    python -m tools.release site-token revoke --client covert --site S0042
    python -m tools.release site-token revoke-client --client covert
    python -m tools.release site-token list --client covert

- El token es `<cliente>.<43 caracteres aleatorios>` (256 bits). Se muestra **una sola vez**; en el KV solo
  se guarda su SHA-256: clave `<cliente>:<sha256 hex>`, valor `{"site", "active", "created"}`.
- Revocar una sede la deja `active: false` (el Worker responde 401); revocar un cliente borra su prefijo.
- El panel central de un cliente **nunca** tiene credenciales de Cloudflare: esto se ejecuta solo en el
  PC de publicación, con `CF_ACCOUNT_ID`, `CF_KV_NAMESPACE_ID` y `CF_API_TOKEN` (token de API con permiso
  solo de escritura en ese KV). Sin cuenta todavía (decisión N3/D5): `--kv-file` guarda en un JSON local con
  el mismo formato (lo usan las pruebas con Miniflare).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
CF_API = "https://api.cloudflare.com/client/v4"


class TokenError(Exception):
    pass


class KV(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...


class LocalKV:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def put(self, key: str, value: str) -> None:
        d = self._load()
        d[key] = value
        self._save(d)

    def delete(self, key: str) -> None:
        d = self._load()
        d.pop(key, None)
        self._save(d)

    def keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self._load() if k.startswith(prefix))


class CloudflareKV:  # pragma: no cover - necesita la cuenta de Cloudflare (decisión D5)
    def __init__(self, account_id: str, namespace_id: str, api_token: str) -> None:
        import httpx

        self.base = f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}"
        self.http = httpx.Client(headers={"Authorization": f"Bearer {api_token}"}, timeout=30)

    @classmethod
    def from_env(cls) -> "CloudflareKV":
        vals = [os.environ.get(v, "") for v in ("CF_ACCOUNT_ID", "CF_KV_NAMESPACE_ID", "CF_API_TOKEN")]
        if not all(vals):
            raise TokenError("Faltan CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID o CF_API_TOKEN (cuenta de Cloudflare, D5). "
                             "Para probar sin cuenta usa --kv-file.")
        return cls(*vals)

    def _key(self, key: str) -> str:
        from urllib.parse import quote
        return quote(key, safe="")

    def get(self, key: str) -> str | None:
        r = self.http.get(f"{self.base}/values/{self._key(key)}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text

    def put(self, key: str, value: str) -> None:
        self.http.put(f"{self.base}/values/{self._key(key)}", content=value.encode()).raise_for_status()

    def delete(self, key: str) -> None:
        r = self.http.delete(f"{self.base}/values/{self._key(key)}")
        if r.status_code not in (200, 404):
            r.raise_for_status()

    def keys(self, prefix: str) -> list[str]:
        out: list[str] = []
        cursor = ""
        while True:
            params = {"prefix": prefix, "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            r = self.http.get(f"{self.base}/keys", params=params)
            r.raise_for_status()
            data = r.json()
            out += [k["name"] for k in data.get("result", [])]
            cursor = (data.get("result_info") or {}).get("cursor") or ""
            if not cursor:
                return out


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check(client: str, site: str | None = None) -> None:
    if not CLIENT_RE.match(client):
        raise TokenError(f"Cliente no válido: {client!r} (minúsculas, cifras y guiones)")
    if site is not None and not SITE_RE.match(site):
        raise TokenError(f"Sede no válida: {site!r}")


def add(kv: KV, client: str, site: str) -> str:
    _check(client, site)
    token = f"{client}.{secrets.token_urlsafe(32)}"
    value = {"site": site, "active": True, "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    kv.put(f"{client}:{token_hash(token)}", json.dumps(value, separators=(",", ":")))
    return token


def _entries(kv: KV, client: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for k in kv.keys(f"{client}:"):
        raw = kv.get(k)
        try:
            out.append((k, json.loads(raw or "{}")))
        except ValueError:
            continue
    return out


def revoke(kv: KV, client: str, site: str) -> int:
    _check(client, site)
    n = 0
    for k, v in _entries(kv, client):
        if v.get("site") == site and v.get("active", False):
            v["active"] = False
            v["revoked"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            kv.put(k, json.dumps(v, separators=(",", ":")))
            n += 1
    return n


def revoke_client(kv: KV, client: str) -> int:
    _check(client)
    keys = kv.keys(f"{client}:")
    for k in keys:
        kv.delete(k)
    return len(keys)


def list_sites(kv: KV, client: str) -> list[dict[str, Any]]:
    _check(client)
    return [{"site": v.get("site"), "active": v.get("active"), "created": v.get("created"),
             "hash_prefix": k.split(":", 1)[1][:12]} for k, v in _entries(kv, client)]


def write_secrets(path: Path, token: str) -> None:
    """`secrets.json` para el instalador (`/SECRETS=`), que lo lee y lo borra. Solo para el propietario."""
    data = {"update_token": token}
    path = Path(path)
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                data = {**prev, **data}
        except ValueError:
            pass
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
