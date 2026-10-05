"""Lo que el actualizador consulta o escribe del sistema operativo, detrás de una interfaz para las pruebas.

- Reinicio pendiente de Windows (`RebootRequired` de Windows Update y `RebootPending` de CBS): si lo hay,
  no se empieza (PLAN-V2 §2.5 paso 4).
- `DisplayVersion` (clave de desinstalación de Inno) e `InstalledVersion` (CONTRATO §13.7).
- Espacio libre en disco.
- Build de Windows (requisito `windows_build_min` del descriptor).
"""
from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger("vms_updater.system")

REG_PRODUCT = r"SOFTWARE\VMSMultimarca"
REG_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
REBOOT_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
)


class SystemInfo(Protocol):
    def reboot_pending(self) -> bool: ...
    def free_bytes(self, path: Path) -> int: ...
    def windows_build(self) -> int | None: ...
    def write_installed_version(self, version: str, app_id: str | None) -> None: ...
    def installed_version(self) -> str | None: ...


class RealSystem:
    def reboot_pending(self) -> bool:
        if sys.platform != "win32":
            return False
        import winreg

        for key in REBOOT_KEYS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                    return True
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.debug("No se pudo leer %s: %s", key, exc)
        return False

    def free_bytes(self, path: Path) -> int:
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(p).free

    def windows_build(self) -> int | None:
        if sys.platform != "win32":
            return None
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]

    def write_installed_version(self, version: str, app_id: str | None) -> None:
        if sys.platform != "win32":
            return
        import winreg

        access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, REG_PRODUCT, 0, access) as k:
            winreg.SetValueEx(k, "InstalledVersion", 0, winreg.REG_SZ, version)
        if app_id:
            key = rf"{REG_UNINSTALL}\{app_id}_is1"
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, access) as k:
                    winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, version)
            except FileNotFoundError:
                log.warning("No existe la clave de desinstalación de Inno (%s): DisplayVersion sin cambiar", key)

    def installed_version(self) -> str | None:
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_PRODUCT, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                value, _ = winreg.QueryValueEx(k, "InstalledVersion")
                return str(value)
        except OSError:
            return None


@dataclass
class FakeSystem:
    """Doble para pruebas (también simula el registro que lee el instalador)."""

    pending_reboot: bool = False
    free: int = 10 ** 12
    build: int | None = 26200
    registry: dict[str, str] = field(default_factory=dict)

    def reboot_pending(self) -> bool:
        return self.pending_reboot

    def free_bytes(self, path: Path) -> int:
        return self.free

    def windows_build(self) -> int | None:
        return self.build

    def write_installed_version(self, version: str, app_id: str | None) -> None:
        self.registry["InstalledVersion"] = version
        if app_id:
            self.registry["DisplayVersion"] = version

    def installed_version(self) -> str | None:
        return self.registry.get("InstalledVersion")


def installer_may_install(installer_version: str, installed: str | None, *, allow_downgrade: bool = False) -> tuple[bool, str]:
    """La regla de `InitializeSetup` del instalador (B3, Pascal) escrita en Python para probarla con el doble
    del registro (PLAN-V2 §4.4): un Setup no baja de versión salvo `/ALLOWDOWNGRADE`."""
    from .versioning import Version

    if not installed:
        return True, ""
    try:
        if Version.parse(installed) > Version.parse(installer_version) and not allow_downgrade:
            return False, (f"Ya está instalada la {installed}, más nueva que este instalador ({installer_version}). "
                           "Para bajar de versión, soporte técnico usa /ALLOWDOWNGRADE.")
    except ValueError:
        return True, ""
    return True, ""
