"""Diario de la actualización `state\\journal.json` (CONTRATO §13.5 y §15.3).

Cada paso se anota **antes** de hacerlo (`begin`) y se marca hecho **después** (`done`), siempre con
escritura atómica. Al arrancar, el actualizador lee el diario y retoma o revierte (`engine.recover`).

Fallos inyectados (solo pruebas, CONTRATO §15.3), con `VMS_UPDATER_TEST_HOOKS=1`:
- `VMS_UPDATER_FAULT_AT=<estado>[:before|:after]` mata el proceso (`os._exit(86)`) justo antes de hacer el
  paso (con el paso ya anotado) o justo después (hecho, pero sin marcarlo). Sin sufijo, en los dos.
- `VMS_UPDATER_PAUSE_AT=<estado>` espera 30 s (`VMS_UPDATER_PAUSE_S`) en ese estado y lo anuncia en
  `public-status.json` (`paused_at`), para los cortes de luz de verdad en Hyper-V (PLAN-V2 §4.4 bis).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ._atomic import atomic_write_json, read_json
from .models import Journal, JournalStep

log = logging.getLogger("vms_updater.journal")

FAULT_EXIT_CODE = 86


class SimulatedCrash(BaseException):
    """Fallo inyectado dentro del proceso (pruebas): sale de todo como un `kill`."""


FaultHook = Callable[[str, str], None]   # (estado, "before"|"after")


def env_fault_hook() -> FaultHook | None:
    """Gancho de fallos desde el entorno (solo si VMS_UPDATER_TEST_HOOKS=1)."""
    if os.environ.get("VMS_UPDATER_TEST_HOOKS") != "1":
        return None
    fault = os.environ.get("VMS_UPDATER_FAULT_AT", "").strip()
    pause = os.environ.get("VMS_UPDATER_PAUSE_AT", "").strip()
    if not fault and not pause:
        return None
    f_state, _, f_phase = fault.partition(":")
    pause_s = float(os.environ.get("VMS_UPDATER_PAUSE_S", "30"))

    def hook(state: str, phase: str) -> None:
        if pause and state == pause and phase == "before":
            log.warning("PRUEBA: pausa de %.0f s en «%s» (VMS_UPDATER_PAUSE_AT)", pause_s, state)
            announce = getattr(hook, "announce", None)
            if callable(announce):
                announce(state)
            time.sleep(pause_s)
        if fault and state == f_state and (not f_phase or f_phase == phase):
            log.warning("PRUEBA: fallo inyectado en «%s» (%s)", state, phase)
            logging.shutdown()
            os._exit(FAULT_EXIT_CODE)

    return hook


class JournalStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time,
                 fault_hook: FaultHook | None = None) -> None:
        self.path = Path(path)
        self.clock = clock
        self.fault_hook = fault_hook

    def read(self) -> Journal | None:
        raw = read_json(self.path)
        if raw is None:
            if self.path.exists():
                self._quarantine()
            return None
        try:
            return Journal.model_validate(raw)
        except ValidationError as exc:
            log.error("journal.json no es válido (%d errores)", exc.error_count())
            self._quarantine()
            return None

    def _quarantine(self) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(self.clock()))
        try:
            shutil.move(str(self.path), str(self.path.with_name(f"journal.corrupt-{stamp}.json")))
        except OSError as exc:
            log.warning("No se pudo apartar el diario dañado: %s", exc)

    def write(self, j: Journal) -> Journal:
        atomic_write_json(self.path, j.dump())
        return j

    # ------------------------------------------------------------------ pasos
    def begin(self, j: Journal, state: str) -> Journal:
        """Anota el paso ANTES de hacerlo."""
        step = JournalStep(state=state, started_unix=int(self.clock()))
        j = j.model_copy(update={"state": state, "steps": [*j.steps, step]})
        self.write(j)
        self._hook(state, "before")
        return j

    def done(self, j: Journal) -> Journal:
        """Marca hecho el paso en curso DESPUÉS de hacerlo."""
        step = j.current_step
        if step is None:
            return j
        self._hook(step.state, "after")
        steps = [*j.steps[:-1], step.model_copy(update={"done_unix": int(self.clock())})]
        j = j.model_copy(update={"steps": steps})
        return self.write(j)

    def step_done(self, j: Journal, state: str) -> bool:
        """¿El último paso anotado con `state` está marcado como hecho?"""
        for s in reversed(j.steps):
            if s.state == state:
                return s.done_unix is not None
        return False

    def _hook(self, state: str, phase: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(state, phase)

    def set(self, j: Journal, **changes: Any) -> Journal:
        return self.write(j.model_copy(update=changes))
