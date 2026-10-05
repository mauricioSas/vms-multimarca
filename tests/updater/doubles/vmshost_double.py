"""Reglas de `vmshost` sobre el puntero (CONTRATO §13.3), en Python, para las pruebas del actualizador.

`vmshost.exe` (B1, Rust) es quien las aplica en el producto; aquí se reproducen para comprobar que el
actualizador deja siempre lo que `vmshost` necesita (puntero válido o reconstruible y `last_good` en el
diario) y que reacciona bien cuando `vmshost` vuelve atrás.
"""
from __future__ import annotations

from vms_updater.journal import JournalStore
from vms_updater.layout import Layout
from vms_updater.pointer import PointerStore, rebuild_pointer

CRASHES = 3
TRIAL_TIMEOUT_S = 30 * 60


def ensure_pointer(layout: Layout, now: float) -> str:
    ps = PointerStore(layout.pointer_file, clock=lambda: now)
    if ps.read() is not None:
        return "ok"
    ptr = rebuild_pointer(JournalStore(layout.journal_file).read(), layout.versions_dir, clock=lambda: now)
    if ptr is None:
        return "sin versión"
    ps.write(ptr)
    return f"reconstruido:{ptr.active}"


def watch_updater(layout: Layout, *, crashes: int, now: float) -> str:
    """Si la ranura nueva del actualizador cae 3 veces o no confirma en 30 min, vuelve a la anterior."""
    ps = PointerStore(layout.pointer_file, clock=lambda: now)
    ptr = ps.read()
    if ptr is None or not ptr.updater.trial:
        return "nada"
    since = ptr.updater.trial_since_unix or now
    if crashes >= CRASHES or now - since >= TRIAL_TIMEOUT_S:
        prev = ptr.updater.previous_slot or ("a" if ptr.updater.slot == "b" else "b")
        upd = ptr.updater.model_copy(update={"slot": prev, "previous_slot": ptr.updater.slot, "trial": False,
                                             "trial_since_unix": None})
        ps.write(ptr.model_copy(update={"updater": upd}))
        return f"vuelta a la ranura {prev}"
    return "esperando"


def watch_version(layout: Layout, *, crashes: int, now: float) -> str:
    """Versión a prueba que cae 3 veces o no se confirma: vuelve a `previous`."""
    ps = PointerStore(layout.pointer_file, clock=lambda: now)
    ptr = ps.read()
    if ptr is None or not ptr.trial or not ptr.previous:
        return "nada"
    since = ptr.trial_since_unix or now
    if crashes >= CRASHES or now - since >= TRIAL_TIMEOUT_S:
        ps.write(ptr.model_copy(update={"active": ptr.previous, "previous": ptr.active, "trial": False,
                                        "trial_since_unix": None}))
        return f"vuelta a {ptr.previous}"
    return "esperando"
