"""Arranque con la sesión de Windows (clave Run del usuario actual, sin administrador).

Lo usa el lanzador del kiosco cuando no se instala como tarea programada.
El backend se instala como servicio (ver deploy/windows), no por aquí.
"""
from __future__ import annotations

import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def supported() -> bool:
    return sys.platform == "win32"


def is_enabled(value_name: str) -> bool:
    if sys.platform != "win32":   # (comprobación literal: así mypy sabe que winreg solo se usa en Windows)
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, value_name)
            return True
    except OSError:
        return False


def set_enabled(value_name: str, command: str | None, enabled: bool) -> None:
    """Activa (con `command`, ya entrecomillado) o desactiva el arranque automático."""
    if sys.platform != "win32":
        raise RuntimeError("El arranque automático solo está disponible en Windows")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            if not command:
                raise ValueError("Falta el comando de arranque")
            winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, value_name)
            except FileNotFoundError:
                pass
