"""Nombres compartidos entre motor, API y analítica (fuente única).

Rutas de MediaMTX por cámara:
    <camera_id>/main   flujo principal: se graba 24/7 (conexión permanente al equipo)
    <camera_id>/sub    subflujo: vista en vivo y analítica (bajo demanda)
Si la cámara no tiene subflujo, la vista en vivo y la analítica usan <camera_id>/main.
"""
from __future__ import annotations

import re
import secrets
from typing import Literal

ID_PATTERN = r"^[a-z0-9][a-z0-9\-]{2,39}$"
_ID_RE = re.compile(ID_PATTERN)

IdPrefix = Literal["dev", "cam", "rule", "site"]


def new_id(prefix: IdPrefix) -> str:
    """Identificador corto, estable y apto para rutas de MediaMTX y nombres de carpeta."""
    return f"{prefix}-{secrets.token_hex(4)}"


def is_valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))


def mtx_path(camera_id: str, stream: Literal["main", "sub"]) -> str:
    if not is_valid_id(camera_id):
        raise ValueError(f"Identificador de cámara no válido: {camera_id!r}")
    if stream not in ("main", "sub"):
        raise ValueError(f"Flujo no válido: {stream!r}")
    return f"{camera_id}/{stream}"


def parse_mtx_path(path: str) -> tuple[str, str] | None:
    """Inverso de mtx_path: «cam-1a2b3c4d/sub» → ("cam-1a2b3c4d", "sub"); None si no es nuestra."""
    parts = (path or "").strip("/").split("/")
    if len(parts) == 2 and is_valid_id(parts[0]) and parts[1] in ("main", "sub"):
        return parts[0], parts[1]
    return None
