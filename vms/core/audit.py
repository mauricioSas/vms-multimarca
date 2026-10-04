"""Registro de accesos a imágenes de videovigilancia (RGPD art. 32 y guías de la AEPD).

Cada visualización o descarga de una grabación, cada captura y cada vista en vivo a resolución
completa deja una línea con usuario, IP, cámara y tramo. El archivo es `logs/audit.log` (rotativo,
10 MB x 20) y la carpeta de registros solo es accesible para SYSTEM/Administradores (Windows) o el
usuario del servicio (Linux): los usuarios de la interfaz web no pueden borrarlo.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .logging_setup import AUDIT_LOGGER

_log = logging.getLogger(AUDIT_LOGGER)


def audit(event: str, *, user: str, ip: str, **fields: Any) -> None:
    """Una línea JSON por acceso (fácil de filtrar o entregar ante una petición de un afectado)."""
    data = {"event": event, "user": user, "ip": ip, **fields}
    _log.info("%s", json.dumps(data, ensure_ascii=False, default=str, sort_keys=False))
