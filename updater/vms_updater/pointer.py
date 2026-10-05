"""Puntero de versión `state\\active.json` (CONTRATO §13.4).

Lo escriben solo el actualizador (LocalSystem) y el instalador, siempre con escritura atómica. `vmshost`
lo lee al lanzar cada servicio y, si la versión a prueba falla, vuelve a `previous` (o deja
`state\\rollback-request.json` si corre con una cuenta sin permiso de escritura, CONTRATO §13.3).
Los campos que no se conocen se conservan.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ._atomic import atomic_write_json, read_json
from .models import ActivePointer, Journal, UpdaterSlot

log = logging.getLogger("vms_updater.pointer")


class PointerStore:
    def __init__(self, path: Path, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.clock = clock

    def read(self) -> ActivePointer | None:
        """El puntero, o None si falta o está corrupto."""
        raw = read_json(self.path)
        if raw is None:
            return None
        try:
            return ActivePointer.model_validate(raw)
        except ValidationError as exc:
            log.error("active.json no es válido (%d errores): se tratará como ausente", exc.error_count())
            return None

    def write(self, ptr: ActivePointer) -> ActivePointer:
        ptr = ptr.model_copy(update={"updated_unix": int(self.clock())})
        atomic_write_json(self.path, ptr.dump())
        return ptr

    # ------------------------------------------------------------------ operaciones
    def switch(self, to: str, *, trial: bool = True) -> ActivePointer:
        """Activa `to`; la activa de ahora pasa a `previous`. Con `trial`, vmshost la vigila."""
        cur = self.read()
        now = int(self.clock())
        if cur is None:
            new = ActivePointer(active=to, previous=None, trial=trial, trial_since_unix=now if trial else None)
        elif cur.active == to:
            new = cur.model_copy(update={"trial": trial, "trial_since_unix": (cur.trial_since_unix or now)
                                         if trial else None})
        else:
            new = cur.model_copy(update={"active": to, "previous": cur.active, "trial": trial,
                                         "trial_since_unix": now if trial else None})
        return self.write(new)

    def confirm(self) -> ActivePointer | None:
        cur = self.read()
        if cur is None:
            return None
        if not cur.trial:
            return cur
        return self.write(cur.model_copy(update={"trial": False, "trial_since_unix": None}))

    def set_updater_slot(self, slot: str, *, trial: bool) -> ActivePointer:
        cur = self.read()
        if cur is None:
            raise RuntimeError("No hay puntero de versión: no se puede cambiar la ranura del actualizador")
        now = int(self.clock())
        old = cur.updater
        prev = old.slot if old.slot != slot else old.previous_slot
        if slot not in ("a", "b"):
            raise ValueError(f"ranura no válida: {slot!r}")
        upd = UpdaterSlot.model_validate({"slot": slot, "previous_slot": prev, "trial": trial,
                                          "trial_since_unix": now if trial else None})
        return self.write(cur.model_copy(update={"updater": upd}))

    def confirm_updater(self) -> ActivePointer | None:
        cur = self.read()
        if cur is None or not cur.updater.trial:
            return cur
        upd = cur.updater.model_copy(update={"trial": False, "trial_since_unix": None})
        return self.write(cur.model_copy(update={"updater": upd}))


def rebuild_pointer(journal: Journal | None, versions_dir: Path, *, clock: Any = time.time,
                    known_good: Sequence[str | None] = (),
                    unverified: Collection[str] | None = frozenset()) -> ActivePointer | None:
    """Reconstruye el puntero si falta o está corrupto (lo mismo que hace `vmshost`, CONTRATO §13.3).

    Orden: (1) la última versión buena del diario y las demás conocidas como buenas (`known_good`: registro
    `InstalledVersion` y la lista de versiones que llegaron a `good`); (2) la instalada más alta que NO esté
    en `unverified` (montadas por el actualizador y todavía sin aplicar ni comprobar: p. ej. en espera de la
    ventana); (3) si no queda otra, la más alta, pero **a prueba** (`trial`) y con la siguiente como
    `previous`, para que `vmshost` la vigile y pueda volver atrás. `unverified=None` = no se sabe cuáles
    están sin comprobar (registro perdido): todo lo que no sea conocido como bueno va a prueba."""
    vdir = Path(versions_dir)
    try:
        installed = sorted((p.name for p in vdir.iterdir()
                            if p.is_dir() and not p.name.endswith(".tmp") and (p / "release.json").is_file()),
                           key=_version_key, reverse=True)
    except OSError:
        installed = []
    if not installed:
        return None
    now = int(clock())
    good = [v for v in ([journal.last_good] if journal is not None else []) + list(known_good) if v]
    for v in good:
        if v in installed:
            return ActivePointer(active=v, previous=None, trial=False, updated_unix=now)
    if unverified is not None:
        for v in installed:
            if v not in unverified:
                return ActivePointer(active=v, previous=None, trial=False, updated_unix=now)
    active = installed[0]
    previous = installed[1] if len(installed) > 1 else None
    log.warning("Puntero reconstruido hacia la %s sin saber si es buena: queda a prueba", active)
    return ActivePointer(active=active, previous=previous, trial=True, trial_since_unix=now, updated_unix=now)


def _version_key(name: str) -> Any:
    from .versioning import Version
    try:
        return (1, Version.parse(name))
    except ValueError:
        return (0, name)
