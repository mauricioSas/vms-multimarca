"""Doble de `vmsctl` con la misma CLI (CONTRATO §14) para `services`, `version` y `health`.

    python vmsctl_double.py services stop|start|restart --only VMSBackend,VMSEngine --json
    python vmsctl_double.py version switch 2.1.0 --json | version show --json
    python vmsctl_double.py health wait --timeout 5 --json

Carpetas por `VMS_DATA_DIR` y `VMS_INSTALL_DIR` (como el servicio). El estado de los «servicios» se guarda en
`<datos>/state/services-double.json`: `{"running": {"VMSBackend": "2.0.0", …}, "calls": [...]}`; cada
servicio arranca con la versión activa del puntero en ese momento (como hace `vmshost`). Fallos a propósito:
`VMS_DOUBLE_FAIL=start` (o `stop`) → código 20 con el JSON de error del contrato.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
for _p in (HERE.parents[3] / "updater", HERE.parents[3]):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from vms_updater._atomic import atomic_write_json, read_json  # noqa: E402
from vms_updater.layout import Layout  # noqa: E402
from vms_updater.pointer import PointerStore  # noqa: E402
from vms_updater.services import ServiceError  # noqa: E402


def state_file(layout: Layout) -> Path:
    return layout.state_dir / "services-double.json"


def load_state(layout: Layout) -> dict[str, Any]:
    raw = read_json(state_file(layout))
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("running", {})
    raw.setdefault("calls", [])
    return raw


def save_state(layout: Layout, st: dict[str, Any]) -> None:
    atomic_write_json(state_file(layout), st)


def do_services(layout: Layout, action: str, only: list[str]) -> dict[str, Any]:
    fail = os.environ.get("VMS_DOUBLE_FAIL", "")
    if fail and fail == action:
        raise ServiceError(f"Fallo simulado en «services {action}»", code=20, error_code="windows_error")
    st = load_state(layout)
    ptr = PointerStore(layout.pointer_file).read()
    active = ptr.active if ptr else None
    for s in only:
        if action in ("stop", "restart"):
            st["running"].pop(s, None)
        if action in ("start", "restart"):
            if active is None:
                raise ServiceError("No hay versión activa", code=20, error_code="no_active_version")
            st["running"][s] = active
    st["calls"].append({"action": action, "only": only, "active": active})
    save_state(layout, st)
    return {"running": st["running"]}


class DoubleServices:
    """Mismo comportamiento que la CLI, dentro del proceso (para las pruebas rápidas)."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    def stop(self, services: list[str]) -> None:
        if services:
            do_services(self.layout, "stop", list(services))

    def start(self, services: list[str]) -> None:
        if services:
            do_services(self.layout, "start", list(services))

    def restart(self, services: list[str]) -> None:
        if services:
            do_services(self.layout, "restart", list(services))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vmsctl")
    ap.add_argument("group")
    ap.add_argument("action")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    layout = Layout.from_env()

    def out(ok: bool, code: int, data: Any = None, err: dict[str, Any] | None = None) -> int:
        sys.stdout.write(json.dumps({"ok": ok, "code": code, "data": data, "error": err}) + "\n")
        return code

    try:
        if a.group == "services" and a.action in ("start", "stop", "restart"):
            only = [s for s in a.only.split(",") if s]
            return out(True, 0, do_services(layout, a.action, only))
        if a.group == "version" and a.action == "switch" and a.arg:
            ptr = PointerStore(layout.pointer_file).switch(a.arg, trial=True)
            return out(True, 0, ptr.dump())
        if a.group == "version" and a.action == "show":
            ptr = PointerStore(layout.pointer_file).read()
            return out(True, 0, ptr.dump() if ptr else None)
        if a.group == "health" and a.action == "wait":
            from tests.updater.doubles.fake_backend import deep_health
            ok = deep_health(layout) is not None
            return out(ok, 0 if ok else 12, None, None if ok else {"code": "health_failed",
                                                                  "message_es": "health check fallido"})
    except ServiceError as exc:
        return out(False, exc.code or 20, None, {"code": exc.error_code, "message_es": exc.message_es, "win32": None})
    return out(False, 2, None, {"code": "usage", "message_es": "uso incorrecto"})


if __name__ == "__main__":
    sys.exit(main())
