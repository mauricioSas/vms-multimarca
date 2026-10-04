"""Construcción de URLs RTSP y presets por fabricante.

Presets a partir del número de canal N:
  Hikvision: /Streaming/Channels/{N}01 (principal) y /Streaming/Channels/{N}02 (subflujo)
  Dahua:     /cam/realmonitor?channel={N}&subtype=0 (principal) y subtype=1 (subflujo)
  Genérico/ONVIF: ruta manual.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote

VENDOR_HIKVISION = "hikvision"
VENDOR_DAHUA = "dahua"
VENDOR_GENERIC = "generic"

VENDORS = {
    VENDOR_HIKVISION: "Hikvision",
    VENDOR_DAHUA: "Dahua",
    VENDOR_GENERIC: "Genérico / ONVIF",
}

STREAM_MAIN = "main"
STREAM_SUB = "sub"


def preset_paths(vendor: str, channel: int) -> tuple[str, str] | None:
    """Devuelve (ruta_principal, ruta_subflujo) del fabricante o None si es genérico."""
    channel = int(channel)
    if channel < 1:
        raise ValueError("El canal debe ser 1 o mayor")
    if vendor == VENDOR_HIKVISION:
        return (f"/Streaming/Channels/{channel}01", f"/Streaming/Channels/{channel}02")
    if vendor == VENDOR_DAHUA:
        return (
            f"/cam/realmonitor?channel={channel}&subtype=0",
            f"/cam/realmonitor?channel={channel}&subtype=1",
        )
    return None


def normalize_path(path: str) -> str:
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


_CRED_RE = re.compile(r"(rtsps?://)([^/@\s]*)@")


def redact(text: str) -> str:
    """Oculta las credenciales de cualquier URL RTSP dentro de un texto (para registros)."""
    if not text:
        return text
    return _CRED_RE.sub(lambda m: m.group(1) + "***:***@", text)


_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$")


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
