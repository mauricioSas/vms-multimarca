"""Archivos de estado pequeños del actualizador en `<datos>\\updater\\`.

- `public-status.json` (CONTRATO §15.6): lo leen el visor, `/status` y el latido. Nunca lleva secretos.
- `blacklist.json`: versiones que fallaron y no se reintentan hasta que haya otra mayor.
- `updater.json`: canal, retener y ventana de la sede (instalador y panel central).
- `central-directive.json`: lo último que pidió el panel central en la respuesta del latido.
- `install-lock.json`: cerrojo compartido con el instalador (`lock`/`unlock` por la tubería, §15.2).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ._atomic import atomic_write_json, read_json
from .models import CentralDirective, LocalUpdaterConfig, PublicStatus, iso, utcnow
from .versioning import Version

log = logging.getLogger("vms_updater.state")


class StatusFile:
    def __init__(self, path: Path, *, now: Callable[[], datetime] = utcnow) -> None:
        self.path = Path(path)
        self.now = now

    def read(self) -> PublicStatus:
        raw = read_json(self.path)
        if isinstance(raw, dict):
            try:
                return PublicStatus.model_validate(raw)
            except ValidationError:
                log.warning("public-status.json no válido: se rehace")
        return PublicStatus()

    def update(self, **changes: Any) -> PublicStatus:
        cur = self.read()
        new = cur.model_copy(update={**changes, "updated": iso(self.now())})
        atomic_write_json(self.path, new.dump())
        return new


class Blacklist:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        raw = read_json(self.path)
        return raw if isinstance(raw, dict) and isinstance(raw.get("versions"), dict) else {"schema": 1, "versions": {}}

    def add(self, version: str, reason: str) -> None:
        data = self._load()
        data["versions"][version] = {"reason": reason[:300], "at": iso(utcnow())}
        atomic_write_json(self.path, data)

    def contains(self, version: str) -> bool:
        return version in self._load()["versions"]

    def versions(self) -> list[str]:
        return sorted(self._load()["versions"])

    def blocks(self, candidate: str) -> bool:
        """Una versión en la lista negra no se reintenta; una MAYOR que todas las de la lista sí."""
        return self.contains(candidate)


def read_local_config(path: Path) -> LocalUpdaterConfig:
    raw = read_json(path)
    if isinstance(raw, dict):
        try:
            return LocalUpdaterConfig.model_validate(raw)
        except ValidationError as exc:
            log.error("updater.json no válido (%d errores): se usan los valores por defecto", exc.error_count())
    return LocalUpdaterConfig()


def write_local_config(path: Path, cfg: LocalUpdaterConfig) -> None:
    atomic_write_json(path, cfg.model_dump(mode="json"))


def read_directive(path: Path) -> CentralDirective | None:
    raw = read_json(path)
    if not isinstance(raw, dict):
        return None
    try:
        return CentralDirective.model_validate(raw)
    except ValidationError:
        log.warning("central-directive.json no válido: se ignora")
        return None


# --------------------------------------------------------------------------- ventana de mantenimiento
def parse_window(text: str) -> tuple[dtime, dtime]:
    a, _, b = text.partition("-")
    start = datetime.strptime(a.strip(), "%H:%M").time()
    end = datetime.strptime(b.strip(), "%H:%M").time()
    return start, end


def in_window(local_now: datetime, window: str) -> bool:
    """¿La hora local está dentro de la ventana? Admite ventanas que cruzan medianoche (23:00-02:00)."""
    start, end = parse_window(window)
    t = local_now.time().replace(tzinfo=None)
    if start == end:
        return True                     # "00:00-00:00" = siempre
    if start < end:
        return start <= t < end
    return t >= start or t < end


def next_window_start(local_now: datetime, window: str) -> datetime:
    start, _ = parse_window(window)
    cand = local_now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if cand <= local_now:
        cand += timedelta(days=1)
    return cand


# --------------------------------------------------------------------------- cerrojo con el instalador
class InstallLock:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock

    def holder(self) -> str | None:
        raw = read_json(self.path)
        if not isinstance(raw, dict):
            return None
        if float(raw.get("expires_unix", 0)) < self.clock():
            return None
        owner = raw.get("owner")
        return str(owner) if owner else None

    def acquire(self, owner: str, ttl_s: int) -> bool:
        cur = self.holder()
        if cur is not None and cur != owner:
            return False
        atomic_write_json(self.path, {"owner": owner, "expires_unix": int(self.clock()) + max(1, min(ttl_s, 6 * 3600))})
        return True

    def release(self, owner: str) -> bool:
        cur = self.holder()
        if cur is not None and cur != owner:
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True


def sort_versions(versions: list[str]) -> list[str]:
    return sorted(versions, key=Version.parse)
