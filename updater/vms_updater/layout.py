"""Rutas del actualizador (CONTRATO §13.1). Sin depender de `vms.core.paths`.

- Carpeta de instalación (`C:\\Program Files\\VMSMultimarca`): `bin\\vmshost.exe`, `versions\\<X>\\`,
  `updater\\slot-a|b\\` y `updater\\trusted\\online|offline\\1.root.json`.
- Carpeta de datos (`C:\\ProgramData\\VMSMultimarca`): `state\\`, `backups\\`, `updater\\`, `config\\`,
  `secrets\\`, `ops\\advisories\\`.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_ID = "VMSMultimarca"


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
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "vms-multimarca"


def default_install_dir() -> Path:
    override = os.environ.get("VMS_INSTALL_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("ProgramW6432") or os.environ.get("PROGRAMFILES") or r"C:\Program Files"
        return Path(base) / APP_ID
    return default_data_dir() / "install"


@dataclass(frozen=True)
class Layout:
    install: Path
    data: Path

    @classmethod
    def from_env(cls) -> "Layout":
        return cls(default_install_dir(), default_data_dir())

    # ---------------------------------------------------------------- instalación
    @property
    def versions_dir(self) -> Path:
        return self.install / "versions"

    def version_dir(self, version: str) -> Path:
        return self.versions_dir / version

    def staging_dir(self, version: str) -> Path:
        return self.versions_dir / f"{version}.tmp"

    @property
    def updater_root(self) -> Path:
        return self.install / "updater"

    def slot_dir(self, slot: str) -> Path:
        return self.updater_root / f"slot-{slot}"

    def trusted_root_file(self, mode: str) -> Path:
        return self.updater_root / "trusted" / mode / "1.root.json"

    # ---------------------------------------------------------------- datos
    @property
    def state_dir(self) -> Path:
        return self.data / "state"

    @property
    def pointer_file(self) -> Path:
        return self.state_dir / "active.json"

    @property
    def journal_file(self) -> Path:
        return self.state_dir / "journal.json"

    @property
    def rollback_request_file(self) -> Path:
        return self.state_dir / "rollback-request.json"

    @property
    def backups_dir(self) -> Path:
        return self.data / "backups"

    @property
    def updater_data(self) -> Path:
        return self.data / "updater"

    @property
    def public_status_file(self) -> Path:
        return self.updater_data / "public-status.json"

    @property
    def local_config_file(self) -> Path:
        return self.updater_data / "updater.json"

    @property
    def directive_file(self) -> Path:
        return self.updater_data / "central-directive.json"

    @property
    def blacklist_file(self) -> Path:
        return self.updater_data / "blacklist.json"

    @property
    def lock_file(self) -> Path:
        return self.updater_data / "install-lock.json"

    @property
    def cache_dir(self) -> Path:
        return self.updater_data / "cache"

    @property
    def tuf_metadata_dir(self) -> Path:
        return self.cache_dir / "metadata"

    @property
    def tuf_targets_dir(self) -> Path:
        return self.cache_dir / "targets"

    @property
    def config_dir(self) -> Path:
        return self.data / "config"

    @property
    def secrets_dir(self) -> Path:
        return self.data / "secrets"

    @property
    def logs_dir(self) -> Path:
        return self.data / "logs"

    @property
    def advisories_file(self) -> Path:
        return self.data / "ops" / "advisories" / "advisories.json"

    def ensure(self) -> "Layout":
        for p in (self.state_dir, self.backups_dir, self.updater_data, self.cache_dir, self.logs_dir,
                  self.versions_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self
