"""Clave Ed25519 de la instalación para firmar los manifiestos de evidencias (CONTRATO §18.7).

- Privada: `<datos>/secrets/evidence-ed25519.key`. Se crea en el primer uso. En Windows, cuando B1 entregue
  `vms.core.winsec` con `protect(bytes) -> bytes` / `unprotect(bytes) -> bytes` (DPAPI de máquina), se
  guarda cifrada con DPAPI (cabecera `DPAPI1`); mientras tanto, PEM PKCS#8 con permisos restringidos
  (la ACL de `secrets\\` la pone el instalador: SYSTEM, Administradores y el servicio).
- Pública: `<datos>/ops/evidence-key.json` (`key_id` = SHA-256 de la clave pública en bruto, en hex). Viaja
  en el latido (`payload.evidence_key`) para que la central la tenga, y dentro de cada paquete.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from vms.core.atomic import atomic_write_bytes, atomic_write_text
from vms.core.paths import AppPaths, restrict_permissions

log = logging.getLogger("vms.ops.evidence.keys")

PRIVATE_NAME = "evidence-ed25519.key"
PUBLIC_NAME = "evidence-key.json"
DPAPI_MAGIC = b"DPAPI1\n"
_lock = threading.Lock()


def key_id_of(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()


def public_pem(public_raw: bytes) -> str:
    pub = Ed25519PublicKey.from_public_bytes(public_raw)
    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


@dataclass(frozen=True)
class EvidenceKey:
    private: Ed25519PrivateKey
    public_raw: bytes

    @property
    def key_id(self) -> str:
        return key_id_of(self.public_raw)

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode("ascii")

    def sign(self, data: bytes) -> bytes:
        return self.private.sign(data)

    def public_pem(self) -> str:
        return public_pem(self.public_raw)


def _winsec() -> Any:
    if sys.platform != "win32":
        return None
    try:
        from vms.core import winsec  # entrega de B1
    except ImportError:
        return None
    if callable(getattr(winsec, "protect", None)) and callable(getattr(winsec, "unprotect", None)):
        return winsec
    return None


def _encode_private(key: Ed25519PrivateKey) -> bytes:
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    ws = _winsec()
    if ws is not None:
        return DPAPI_MAGIC + bytes(ws.protect(pem))
    return pem


def _decode_private(data: bytes) -> Ed25519PrivateKey:
    if data.startswith(DPAPI_MAGIC):
        ws = _winsec()
        if ws is None:
            raise RuntimeError("La clave de firma está cifrada con DPAPI y este equipo no puede descifrarla")
        data = bytes(ws.unprotect(data[len(DPAPI_MAGIC):]))
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("La clave de firma de evidencias no es Ed25519")
    return key


def load_or_create(paths: AppPaths) -> EvidenceKey:
    with _lock:
        priv_file = paths.secrets_dir / PRIVATE_NAME
        if priv_file.is_file():
            key = _decode_private(priv_file.read_bytes())
        else:
            paths.secrets_dir.mkdir(parents=True, exist_ok=True)
            key = Ed25519PrivateKey.generate()
            atomic_write_bytes(priv_file, _encode_private(key))
            restrict_permissions(priv_file)
            log.info("Creada la clave de firma de evidencias de esta instalación")
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ek = EvidenceKey(private=key, public_raw=raw)
        _write_public(paths, ek)
        return ek


def _write_public(paths: AppPaths, ek: EvidenceKey) -> None:
    f = paths.base / "ops" / PUBLIC_NAME
    data = {"algorithm": "ed25519", "key_id": ek.key_id, "public_key": ek.public_b64}
    try:
        current = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if isinstance(current, dict) and current.get("key_id") == ek.key_id:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_text(f, json.dumps(data, indent=1))


def read_public(paths: AppPaths) -> dict[str, str] | None:
    """Clave pública publicada (sin tocar la privada): lo usa el latido."""
    try:
        data = json.loads((paths.base / "ops" / PUBLIC_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("key_id") or not data.get("public_key"):
        return None
    return {"key_id": str(data["key_id"]), "public_key": str(data["public_key"])}
