"""Seguridad del panel central: contraseñas, sesiones, límite de intentos y tokens de sede.

- Usuarios: mismo esquema que el backend (`vms.core.models.User`, hash argon2id) en
  `<datos>/config/users.json`, gestionados con `vms.core.config_store.UserStore`.
- Sesiones: en memoria del proceso (se pierden al reiniciar: hay que volver a entrar).
- Tokens de sede: uno por tienda, para `POST /api/heartbeat`. Solo se guarda su SHA-256
  (los tokens son aleatorios de 256 bits; no hace falta un hash lento). El valor en claro se
  muestra una única vez al crearlo.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from vms.core.atomic import atomic_write_text
from vms.core.models import Role
from vms.core.naming import is_valid_id
from vms.core.paths import restrict_permissions

log = logging.getLogger("central.security")

_hasher = PasswordHasher()
# Hash fijo para igualar tiempos cuando el usuario no existe (evita enumerar usuarios).
_DUMMY_HASH = _hasher.hash("contraseña-de-relleno-para-igualar-tiempos")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# argon2 fuera del bucle asíncrono y como mucho 2 a la vez (un ataque no bloquea el panel).
_HASH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2")


async def verify_password_async(password_hash: str | None, password: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(_HASH_POOL, verify_password, password_hash, password)


async def hash_password_async(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_HASH_POOL, hash_password, password)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =========================================================================== sesiones
@dataclass
class Session:
    token: str
    username: str
    role: Role
    expires_at: datetime


class SessionStore:
    def __init__(self, hours: int) -> None:
        self._ttl = timedelta(hours=hours)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str, role: Role) -> Session:
        s = Session(secrets.token_urlsafe(32), username, role, utcnow() + self._ttl)
        with self._lock:
            self._purge()
            self._sessions[s.token] = s
        return s

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                return None
            if s.expires_at <= utcnow():
                self._sessions.pop(token, None)
                return None
            return s

    def delete(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def delete_user(self, username: str) -> None:
        with self._lock:
            for t in [t for t, s in self._sessions.items() if s.username.lower() == username.lower()]:
                del self._sessions[t]

    def update_role(self, username: str, role: Role) -> None:
        with self._lock:
            for s in self._sessions.values():
                if s.username.lower() == username.lower():
                    s.role = role

    def _purge(self) -> None:
        now = utcnow()
        for t in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[t]


# =========================================================================== límite de intentos
@dataclass
class _Window:
    hits: list[float] = field(default_factory=list)


class FailureLimiter:
    """Cuenta fallos por clave en una ventana deslizante (p. ej. 5 fallos en 5 minutos)."""

    def __init__(self, max_failures: int, window_s: float) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._data: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def _clean(self, key: str, now: float) -> _Window:
        w = self._data.setdefault(key, _Window())
        w.hits = [t for t in w.hits if now - t < self.window_s]
        return w

    def retry_after(self, key: str) -> int:
        """0 si se permite; si no, segundos que hay que esperar."""
        now = time.monotonic()
        with self._lock:
            w = self._clean(key, now)
            if len(w.hits) < self.max_failures:
                return 0
            return max(1, int(self.window_s - (now - w.hits[0])) + 1)

    MAX_KEYS = 10_000

    def fail(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._clean(key, now).hits.append(now)
            if len(self._data) > self.MAX_KEYS:  # acota la memoria ante un barrido de claves
                # Antes se vaciaba todo, lo que permitía «resetear» el bloqueo de cualquier usuario
                # generando claves nuevas. Ahora solo se quitan las caducadas y, si aún sobran, las
                # más antiguas.
                for k in [k for k, w in self._data.items() if not w.hits or now - w.hits[-1] >= self.window_s]:
                    del self._data[k]
                excess = len(self._data) - self.MAX_KEYS
                if excess > 0:
                    # Primero se descartan las claves NO bloqueadas y más antiguas: un barrido no puede
                    # «desbloquear» a quien ya llegó al límite.
                    order = sorted(self._data, key=lambda k: (len(self._data[k].hits) >= self.max_failures,
                                                              self._data[k].hits[-1]))
                    for k in order[:excess]:
                        del self._data[k]

    def reset(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


# =========================================================================== tokens de sede
def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SiteTokenStore:
    """`site_tokens.json`: {"sites": {"<site_id>": {"sha256", "created_at", "last_used_at"}}}.

    Se recarga sola si otro proceso (la CLI) cambia el archivo mientras el panel está en marcha.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._sites: dict[str, dict[str, Any]] = {}
        self._by_hash: dict[str, str] = {}
        self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._sites, self._by_hash, self._mtime = {}, {}, None
            return
        if mtime == self._mtime:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            sites = {k: v for k, v in (raw.get("sites") or {}).items()
                     if is_valid_id(k) and isinstance(v, dict) and isinstance(v.get("sha256"), str)}
        except (OSError, ValueError, AttributeError) as exc:
            log.error("site_tokens.json no se puede leer (%s); se mantienen los tokens cargados", exc)
            return
        self._sites = sites
        self._by_hash = {v["sha256"]: k for k, v in sites.items()}
        self._mtime = mtime

    def _write(self) -> None:
        data = {"sites": self._sites}
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))
        restrict_permissions(self.path)
        self._mtime = self.path.stat().st_mtime

    def issue(self, site_id: str) -> str:
        """Crea o rota el token de una sede. Devuelve el valor en claro (mostrar una sola vez)."""
        if not is_valid_id(site_id):
            raise ValueError("Identificador de sede no válido")
        token = "vms_" + secrets.token_urlsafe(32)
        with self._lock:
            self._reload_if_changed()
            self._sites[site_id] = {"sha256": _sha256(token), "created_at": utcnow().isoformat(),
                                    "last_used_at": None}
            self._by_hash = {v["sha256"]: k for k, v in self._sites.items()}
            self._write()
        return token

    def revoke(self, site_id: str) -> bool:
        with self._lock:
            self._reload_if_changed()
            if site_id not in self._sites:
                return False
            del self._sites[site_id]
            self._by_hash = {v["sha256"]: k for k, v in self._sites.items()}
            self._write()
        return True

    def verify(self, token: str | None) -> str | None:
        """Devuelve el site_id dueño del token, o None."""
        if not token:
            return None
        digest = _sha256(token)
        with self._lock:
            self._reload_if_changed()
            site_id = self._by_hash.get(digest)
            if site_id is None:
                return None
            # comparación en tiempo constante (defensa adicional)
            if not hmac.compare_digest(self._sites[site_id]["sha256"], digest):
                return None
            return site_id

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reload_if_changed()
            return [{"site_id": k, "created_at": v.get("created_at")} for k, v in sorted(self._sites.items())]
