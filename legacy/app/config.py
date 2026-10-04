"""Configuración persistente con escritura atómica, copia de seguridad y recuperación.

- Se escribe primero en un archivo temporal de la misma carpeta, se hace fsync y se
  sustituye con os.replace (atómico en NTFS y en sistemas POSIX). Un corte de luz deja
  el archivo anterior o el nuevo, nunca uno a medias.
- Antes de sustituir, la versión anterior válida se copia a config.json.bak.
- Si config.json está dañado, se aparta como config.corrupt-FECHA.json (nunca se
  sobrescribe), se intenta cargar la copia .bak y se devuelve un aviso para el usuario.
- En el JSON NO se guardan contraseñas (van al almacén de credenciales del sistema).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import MAX_WALLS, Device, Settings, WallLayout

log = logging.getLogger(__name__)

CONFIG_VERSION = 2
CONFIG_NAME = "config.json"


class ConfigError(ValueError):
    pass


@dataclass
class AppConfig:
    devices: list[Device] = field(default_factory=list)
    walls: list[WallLayout] = field(default_factory=lambda: [WallLayout() for _ in range(MAX_WALLS)])
    wall_screens: list[str] = field(default_factory=lambda: [""] * MAX_WALLS)
    settings: Settings = field(default_factory=Settings)

    def device(self, device_id: str | None) -> Device | None:
        if not device_id:
            return None
        for d in self.devices:
            if d.id == device_id:
                return d
        return None

    def remove_device(self, device_id: str) -> None:
        self.devices = [d for d in self.devices if d.id != device_id]
        for w in self.walls:
            w.cells = [None if c == device_id else c for c in w.cells]

    def to_dict(self) -> dict:
        return {
            "version": CONFIG_VERSION,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "devices": [d.to_dict() for d in self.devices],
            "walls": [{"grid": w.grid, "cells": list(w.cells), "screen": s}
                      for w, s in zip(self.walls, self.wall_screens)],
            "settings": self.settings.__dict__.copy(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> tuple["AppConfig", list[str]]:
        """Convierte el JSON en configuración. Lanza ConfigError si la estructura no es válida."""
        if not isinstance(data, dict):
            raise ConfigError("La raíz del archivo no es un objeto JSON")
        devices_raw = data.get("devices", [])
        walls_raw = data.get("walls", [])
        if not isinstance(devices_raw, list) or not isinstance(walls_raw, list):
            raise ConfigError("Faltan las listas de dispositivos o monitores")
        problems: list[str] = []
        cfg = cls()
        seen = set()
        for i, raw in enumerate(devices_raw):
            try:
                dev = Device.from_dict(raw)
            except (TypeError, ValueError) as exc:
                problems.append(f"Dispositivo #{i + 1} descartado: {exc}")
                continue
            if dev.id in seen:
                problems.append(f"Dispositivo duplicado descartado: {dev.name}")
                continue
            seen.add(dev.id)
            if "password" in raw:
                problems.append(f"Se ignoró una contraseña en texto plano del dispositivo «{dev.name}»; vuelve a escribirla.")
            cfg.devices.append(dev)
        for i in range(MAX_WALLS):
            raw = walls_raw[i] if i < len(walls_raw) and isinstance(walls_raw[i], dict) else {}
            cells = raw.get("cells", [])
            if not isinstance(cells, list):
                cells = []
            cells = [c if (isinstance(c, str) and c in seen) else None for c in cells]
            try:
                grid = int(raw.get("grid", 4))
            except (TypeError, ValueError):
                grid = 4
            cfg.walls[i] = WallLayout(grid, cells).normalized()
            cfg.wall_screens[i] = str(raw.get("screen", "") or "")
        cfg.settings = Settings.from_dict(data.get("settings", {}))
        return cfg, problems


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    last_exc: Exception | None = None
    for _ in range(10):  # en Windows un antivirus puede bloquear el archivo un instante
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.1)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last_exc  # type: ignore[misc]


class ConfigStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path = self.folder / CONFIG_NAME
        self.backup_path = self.folder / (CONFIG_NAME + ".bak")

    # ------------------------------------------------------------------
    def _read(self, path: Path) -> tuple[AppConfig, list[str]]:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return AppConfig.from_dict(data)

    def _quarantine(self, path: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"config.corrupt-{stamp}.json")
        n = 1
        while target.exists():
            target = path.with_name(f"config.corrupt-{stamp}-{n}.json")
            n += 1
        os.replace(path, target)
        return target

    def load(self) -> tuple[AppConfig, str | None]:
        """Carga la configuración. Devuelve (config, aviso_para_el_usuario_o_None)."""
        if not self.path.exists():
            if self.backup_path.exists():
                try:
                    cfg, problems = self._read(self.backup_path)
                    return cfg, "No se encontró config.json; se cargó la copia de seguridad."
                except (OSError, ValueError) as exc:
                    log.warning("Copia de seguridad no válida: %s", exc)
            return AppConfig(), None

        try:
            cfg, problems = self._read(self.path)
        except (OSError, ValueError) as exc:  # json.JSONDecodeError y ConfigError son ValueError
            log.error("config.json dañado: %s", exc)
            moved = self._quarantine(self.path)
            msg = (f"El archivo de configuración estaba dañado ({exc}).\n"
                   f"Se conservó como «{moved.name}» en {self.folder}.\n")
            if self.backup_path.exists():
                try:
                    cfg, _ = self._read(self.backup_path)
                    when = datetime.fromtimestamp(self.backup_path.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
                    return cfg, msg + f"Se cargó la copia de seguridad del {when}. Revisa que esté todo."
                except (OSError, ValueError) as exc2:
                    log.error("La copia de seguridad también está dañada: %s", exc2)
            return AppConfig(), msg + "No había copia de seguridad válida: se empieza con una configuración vacía."

        if problems:
            # Se conserva el original intacto antes de que un guardado lo reescriba sin esos datos.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            keep = self.path.with_name(f"config.original-{stamp}.json")
            shutil.copy2(self.path, keep)
            return cfg, "Se corrigieron problemas en la configuración:\n- " + "\n- ".join(problems) + \
                f"\nEl archivo original se conservó como «{keep.name}»."
        return cfg, None

    def save(self, cfg: AppConfig) -> None:
        text = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2)
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    json.load(f)
                atomic_write_text(self.backup_path, self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("No se actualizó la copia de seguridad: el config.json actual no es válido")
        atomic_write_text(self.path, text)
