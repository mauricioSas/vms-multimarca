"""Rutas de datos de usuario (configuración, registros) y carpeta de grabación por defecto.

La configuración se guarda en la carpeta de datos de la aplicación del usuario:
  - Windows: %APPDATA%\\VMSMultimarca\\VMSMultimarca
  - macOS:   ~/Library/Application Support/VMSMultimarca
  - Linux:   ~/.local/share/VMSMultimarca
Se puede forzar otra carpeta con la variable de entorno VMS_DATA_DIR (útil para pruebas
o para una instalación portable).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID


def _qt_location(kind_name: str) -> str | None:
    try:
        from PySide6.QtCore import QStandardPaths
    except Exception:  # pragma: no cover - Qt siempre está en la app real
        return None
    kind = getattr(QStandardPaths.StandardLocation, kind_name)
    loc = QStandardPaths.writableLocation(kind)
    return loc or None


def data_dir() -> Path:
    override = os.environ.get("VMS_DATA_DIR")
    if override:
        p = Path(override)
    else:
        loc = _qt_location("AppDataLocation")
        if loc:
            p = Path(loc)
        elif sys.platform == "win32":
            p = Path(os.environ.get("APPDATA", Path.home())) / APP_ID
        else:
            p = Path.home() / ".local" / "share" / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_recordings_dir() -> Path:
    loc = _qt_location("MoviesLocation")
    base = Path(loc) if loc else Path.home() / "Videos"
    return base / "VMS-Grabaciones"
