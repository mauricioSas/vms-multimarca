"""URLs RTSP, presets por fabricante y ocultación de credenciales en textos.

Presets a partir del número de canal N (1..512):
  Hikvision: /Streaming/Channels/{N}01 (principal) y /Streaming/Channels/{N}02 (subflujo)
  Dahua:     /cam/realmonitor?channel={N}&subtype=0 (principal) y subtype=1 (subflujo)
  ONVIF:     sin preset fijo; la ruta se obtiene con GetStreamUri y se guarda en la cámara.
  Genérico:  ruta manual.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import quote

VENDOR_HIKVISION = "hikvision"
VENDOR_DAHUA = "dahua"
VENDOR_ONVIF = "onvif"
VENDOR_GENERIC = "generic"

VENDORS: dict[str, str] = {
    VENDOR_HIKVISION: "Hikvision",
    VENDOR_DAHUA: "Dahua",
    VENDOR_ONVIF: "ONVIF",
    VENDOR_GENERIC: "Genérico (RTSP manual)",
}

StreamKind = Literal["main", "sub"]
STREAM_MAIN: StreamKind = "main"
STREAM_SUB: StreamKind = "sub"


def preset_paths(vendor: str, channel: int) -> tuple[str, str] | None:
    """(ruta_principal, ruta_subflujo) del fabricante, o None si no hay preset."""
    channel = int(channel)
    if not 1 <= channel <= 512:
        raise ValueError("El canal debe estar entre 1 y 512")
    if vendor == VENDOR_HIKVISION:
        return (f"/Streaming/Channels/{channel}01", f"/Streaming/Channels/{channel}02")
    if vendor == VENDOR_DAHUA:
        return (
            f"/cam/realmonitor?channel={channel}&subtype=0",
            f"/cam/realmonitor?channel={channel}&subtype=1",
        )
    return None


def normalize_path(path: str | None) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return path


def format_host(host: str) -> str:
    """Pone corchetes a las IPv6 literales para que la URL sea válida."""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]"
    except ValueError:
        pass
    return host


def build_rtsp_url(host: str, port: int, path: str, username: str = "", password: str = "") -> str:
    """URL RTSP con usuario y contraseña codificados (%XX) para que @ : / # ? % no rompan la URL."""
    auth = ""
    if username:
        auth = quote(username, safe="") + ":" + quote(password or "", safe="") + "@"
    return f"rtsp://{auth}{format_host(host)}:{int(port)}{normalize_path(path)}"


# usuario:contraseña@ en cualquier URL (rtsp, rtsps, http, https, postgresql...)
_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s\"']+)@")
# parámetros sensibles en query strings o pares clave=valor
_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pass|pwd|token|api_key|apikey|secret)=([^&\s\"']+)")
# cabeceras Authorization / tokens de bot de Telegram en URLs
_AUTH_HDR_RE = re.compile(r"(?i)(authorization:\s*)(basic|digest|bearer)\s+[^\r\n]+")
_TELEGRAM_RE = re.compile(r"(?i)(api\.telegram\.org/bot)[0-9]+:[A-Za-z0-9_\-]+")


def redact(text: str) -> str:
    """Oculta credenciales de URLs, parámetros y cabeceras dentro de un texto (para registros)."""
    if not text:
        return text
    text = _URL_CRED_RE.sub(lambda m: m.group(1) + "***:***@", text)
    text = _KV_SECRET_RE.sub(lambda m: m.group(1) + "=***", text)
    text = _AUTH_HDR_RE.sub(lambda m: m.group(1) + m.group(2) + " ***", text)
    text = _TELEGRAM_RE.sub(lambda m: m.group(1) + "***", text)
    return text


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$")


def is_valid_host(host: str) -> bool:
    host = (host or "").strip()
    if not host:
        return False
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", host):
        return False  # parece IPv4 pero no es válida (p. ej. 192.168.1.300)
    return bool(_HOSTNAME_RE.match(host))
