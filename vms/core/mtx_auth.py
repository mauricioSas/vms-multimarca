"""Usuarios internos de MediaMTX (API, reproducción y lectura de vídeo).

MediaMTX solo escucha en 127.0.0.1, pero eso no basta: cualquier programa o usuario del propio PC
podría usar su API (que muestra las URL de origen con las contraseñas de los NVR y permite
ejecutar comandos con `runOnInit`) y ver o descargar el vídeo. Por eso no hay usuario anónimo:

- `vms-backend`: API, reproducción, lectura y métricas. Solo lo usa el backend (proxy WHEP, línea
  de tiempo y descargas).
- `vms-reader`: solo lectura RTSP. Lo usa la analítica para leer el subflujo de la cámara.

Las contraseñas se derivan con HMAC-SHA256 del token interno (`VMS_INTERNAL_TOKEN` o
`secrets/internal.token`, que ya comparten backend y analítica): no hay ningún secreto nuevo en
disco y en `mediamtx.yml` solo se escribe su hash SHA-256 (formato `sha256:` de MediaMTX).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

API_USER = "vms-backend"
READER_USER = "vms-reader"
_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")


def _derive(token: str, purpose: str) -> str:
    return hmac.new(token.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).hexdigest()


def mtx_hash(secret: str) -> str:
    """Hash en el formato que MediaMTX acepta en `authInternalUsers` (`sha256:<base64>`)."""
    return "sha256:" + base64.b64encode(hashlib.sha256(secret.encode("utf-8")).digest()).decode("ascii")


@dataclass(frozen=True)
class MtxCredentials:
    api_password: str
    reader_password: str

    @classmethod
    def from_internal_token(cls, token: str) -> "MtxCredentials":
        if not token:
            raise ValueError("Hace falta el token interno para generar las credenciales de MediaMTX")
        return cls(_derive(token, "vms-mediamtx-api-v1"), _derive(token, "vms-mediamtx-reader-v1"))

    @property
    def api_auth(self) -> tuple[str, str]:
        return API_USER, self.api_password

    @property
    def reader_auth(self) -> tuple[str, str]:
        return READER_USER, self.reader_password

    def internal_users(self) -> list[dict[str, Any]]:
        """Bloque `authInternalUsers` de mediamtx.yml (sin «any», sin «publish», solo desde loopback)."""
        local = ("127.0.0.1", "::1")
        return [
            {"user": mtx_hash(API_USER), "pass": mtx_hash(self.api_password), "ips": list(local),
             "permissions": [{"action": "api"}, {"action": "playback"}, {"action": "read"},
                             {"action": "metrics"}]},
            {"user": mtx_hash(READER_USER), "pass": mtx_hash(self.reader_password), "ips": list(local),
             "permissions": [{"action": "read"}]},
        ]


def with_reader_credentials(url: str, creds: MtxCredentials, rtsp_address: str | None = None) -> str:
    """Añade el usuario de lectura a una URL RTSP del MediaMTX local (si no lleva ya usuario).

    Solo para direcciones locales y, si se indica `rtsp_address` (VMS_MTX_RTSP_ADDRESS), solo para
    su puerto: una URL de otro equipo u otro servidor se devuelve tal cual."""
    parts = urlsplit(url)
    if parts.scheme not in ("rtsp", "rtsps") or parts.username or (parts.hostname or "") not in _LOCAL_HOSTS:
        return url
    if rtsp_address:
        _host, _, port = rtsp_address.strip().rpartition(":")
        try:
            if (parts.port or 554) != int(port):
                return url
        except ValueError:
            return url
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    netloc = f"{quote(READER_USER, safe='')}:{quote(creds.reader_password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
