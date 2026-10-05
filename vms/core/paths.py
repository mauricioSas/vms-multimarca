"""Rutas de datos por sistema operativo y localización de binarios externos.

Carpeta de datos por defecto (se puede forzar con VMS_DATA_DIR):
  - Windows: %PROGRAMDATA%\\VMSMultimarca           (compartida por el servicio y el kiosco)
  - macOS:   ~/Library/Application Support/VMSMultimarca   (solo desarrollo)
  - Linux:   $XDG_DATA_HOME/vms-multimarca o ~/.local/share/vms-multimarca
             (el servicio systemd fija VMS_DATA_DIR=/var/lib/vms-multimarca)

Estructura dentro de la carpeta de datos:
  config/      config.json (+ .bak), users.json
  secrets/     clave del almacén cifrado de respaldo, token interno (permisos restringidos)
  logs/        registros rotativos
  recordings/  grabaciones de MediaMTX (si no se configura otra carpeta)
  mediamtx/    mediamtx.yml generado en cada arranque
  analytics/   estado, caché de configuración y cola de reenvío (spool) de la analítica
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from vms import APP_ID

LINUX_DIR_NAME = "vms-multimarca"


def default_data_dir() -> Path:
    override = os.environ.get("VMS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE") or str(Path.home())
        return Path(base) / APP_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / LINUX_DIR_NAME


def install_dir() -> Path:
    """Carpeta de instalación: la del ejecutable congelado o la raíz del repositorio."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppPaths:
    base: Path

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> "AppPaths":
        return cls(Path(data_dir) if data_dir else default_data_dir())

    @property
    def config_dir(self) -> Path:
        return self.base / "config"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def users_file(self) -> Path:
        return self.config_dir / "users.json"

    @property
    def secrets_dir(self) -> Path:
        return self.base / "secrets"

    @property
    def logs_dir(self) -> Path:
        return self.base / "logs"

    @property
    def recordings_dir(self) -> Path:
        return self.base / "recordings"

    @property
    def mediamtx_dir(self) -> Path:
        return self.base / "mediamtx"

    @property
    def analytics_dir(self) -> Path:
        return self.base / "analytics"

    @property
    def updater_data(self) -> Path:
        """`<datos>\\updater` (la escribe solo `VMSUpdater`; en desarrollo, su socket de control `control.sock`)."""
        return self.base / "updater"

    def ensure(self) -> "AppPaths":
        for p in (self.config_dir, self.secrets_dir, self.logs_dir, self.mediamtx_dir, self.analytics_dir):
            p.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self.secrets_dir)
        return self


def restrict_permissions(path: Path) -> None:
    """Deja la carpeta o archivo solo para el usuario actual (POSIX).

    En Windows los permisos (ACL) de la carpeta secrets los fija el instalador con icacls;
    aquí no se hace nada para no depender de pywin32.
    """
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


def exe_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def find_binary(name: str, env_var: str) -> Path | None:
    """Busca un ejecutable: variable de entorno → <instalación>/bin → PATH."""
    override = os.environ.get(env_var)
    if override and Path(override).is_file():
        return Path(override)
    candidate = install_dir() / "bin" / exe_name(name)
    if candidate.is_file():
        return candidate
    which = shutil.which(name)
    return Path(which) if which else None


def find_mediamtx() -> Path | None:
    return find_binary("mediamtx", "VMS_MEDIAMTX_BIN")
