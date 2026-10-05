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
    """Versiones que no se instalan solas (`blacklist.json`).

    - `kind="failed"`: fallaron en este equipo (health check, Authenticode, demasiados cortes).
    - `kind="manual"`: alguien volvió atrás a mano desde ella (tubería, `vmsctl` o «Volver a la anterior» del
      panel). Sin esto, el ciclo siguiente la volvería a instalar. Se levanta con `unskip` (tubería, CLI o
      «Permitir de nuevo» del panel).
    En los dos casos, una versión MAYOR sí se instala (la lista solo bloquea la versión exacta)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        raw = read_json(self.path)
        return raw if isinstance(raw, dict) and isinstance(raw.get("versions"), dict) else {"schema": 1, "versions": {}}

    def add(self, version: str, reason: str, *, kind: str = "failed") -> None:
        data = self._load()
        prev = data["versions"].get(version)
        if kind == "manual" and isinstance(prev, dict) and prev.get("kind", "failed") == "failed":
            return                      # una versión que falló no pasa a «omitida a mano»
        data["versions"][version] = {"reason": reason[:300], "at": iso(utcnow()), "kind": kind}
        atomic_write_json(self.path, data)

    def contains(self, version: str) -> bool:
        return version in self._load()["versions"]

    def kind_of(self, version: str) -> str | None:
        e = self._load()["versions"].get(version)
        if e is None:
            return None
        return str(e.get("kind", "failed")) if isinstance(e, dict) else "failed"

    def versions(self) -> list[str]:
        return sorted(self._load()["versions"])

    def skipped(self) -> list[str]:
        """Versiones omitidas tras una vuelta atrás manual (las que el panel puede «permitir de nuevo»)."""
        data = self._load()["versions"]
        return sort_versions([v for v, e in data.items()
                              if isinstance(e, dict) and e.get("kind") == "manual" and _valid_version(v)])

    def unskip(self, version: str | None = None) -> list[str]:
        """Quita de la lista las omitidas a mano (todas o una). Las que fallaron se quedan."""
        data = self._load()
        gone = [v for v, e in data["versions"].items()
                if isinstance(e, dict) and e.get("kind") == "manual" and (version is None or v == version)]
        for v in gone:
            data["versions"].pop(v)
        if gone:
            atomic_write_json(self.path, data)
        return sort_versions([v for v in gone if _valid_version(v)])

    def blocks(self, candidate: str) -> bool:
        """Una versión de la lista no se reintenta sola; una MAYOR que todas las de la lista sí."""
        return self.contains(candidate)


def _valid_version(v: str) -> bool:
    try:
        Version.parse(v)
    except ValueError:
        return False
    return True


class VersionMarks:
    """`versions-state.json`: versiones que llegaron a «good» y versiones montadas sin comprobar. Sirve para
    reconstruir el puntero sin activar nunca una versión que solo se descargó (CONTRATO §13.3)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return {"schema": 1, "good": [], "pending": []}
        raw = read_json(self.path)
        if isinstance(raw, dict) and isinstance(raw.get("good"), list) and isinstance(raw.get("pending"), list):
            return raw
        return None

    def good(self) -> list[str]:
        data = self._load()
        return [str(v) for v in reversed(data["good"])] if data else []      # la más reciente primero

    def pending(self) -> set[str] | None:
        """Montadas y sin comprobar; None si el archivo está dañado (no se sabe)."""
        data = self._load()
        return {str(v) for v in data["pending"]} if data is not None else None

    def mark_pending(self, version: str) -> None:
        data = self._load() or {"schema": 1, "good": [], "pending": []}
        if version not in data["pending"] and version not in data["good"]:
            data["pending"].append(version)
            atomic_write_json(self.path, data)

    def mark_good(self, version: str) -> None:
        data = self._load() or {"schema": 1, "good": [], "pending": []}
        data["pending"] = [v for v in data["pending"] if v != version]
        data["good"] = [v for v in data["good"] if v != version] + [version]
        data["good"] = data["good"][-10:]
        atomic_write_json(self.path, data)


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
