"""`python -m vms_updater …` — servicio y órdenes del actualizador.

    python -m vms_updater run                      servicio (lo lanza vmsctl run --service VMSUpdater)
    python -m vms_updater check [--force-window] [--no-apply]   una comprobación en este proceso
    python -m vms_updater recover                  retomar o revertir lo que dejó el diario y salir
    python -m vms_updater status                   estado (por la tubería si el servicio corre)
    python -m vms_updater rollback [--to X.Y.Z] [--reason TEXTO]   por la tubería (exige elevación)
    python -m vms_updater pipe '{"cmd": "status"}' petición en bruto por la tubería

Códigos de salida: 0 bien, 1 fallo, 2 uso incorrecto, 3 relanzar con la ranura nueva del actualizador.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading

from .engine import RELOAD_EXIT_CODE
from .layout import Layout


def _print(data: object) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vms_updater")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    c = sub.add_parser("check")
    c.add_argument("--force-window", action="store_true")
    c.add_argument("--no-apply", action="store_true")
    sub.add_parser("recover")
    sub.add_parser("status")
    r = sub.add_parser("rollback")
    r.add_argument("--to")
    r.add_argument("--reason", default="")
    r.add_argument("--local", action="store_true", help="sin tubería (el servicio tiene que estar parado)")
    p = sub.add_parser("pipe")
    p.add_argument("json")
    a = ap.parse_args(argv)

    from .service import build_engine, install_signal_handlers, run_service, setup_logging

    layout = Layout.from_env()
    if a.cmd in ("pipe", "status", "rollback") and not getattr(a, "local", False):
        from .control_pipe import request

        if a.cmd == "pipe":
            try:
                req = json.loads(a.json)
            except ValueError:
                sys.stderr.write("La petición no es JSON válido\n")
                return 2
        elif a.cmd == "status":
            req = {"cmd": "status"}
        else:
            req = {"cmd": "rollback", "to": a.to, "reason": a.reason}
        try:
            resp = request(layout.updater_data, req)
        except OSError as exc:
            if a.cmd == "status":
                from .state_files import StatusFile
                _print({"ok": True, **StatusFile(layout.public_status_file).read().dump(), "service": "down"})
                return 0
            sys.stderr.write(f"No se pudo hablar con el actualizador ({exc}). ¿Está en marcha y la consola "
                             "elevada (administrador)?\n")
            return 1
        _print(resp)
        return 0 if resp.get("ok") else 1

    setup_logging(layout, a.verbose)
    engine = build_engine(layout)
    if a.cmd == "run":
        stop = threading.Event()
        install_signal_handlers(stop)
        return run_service(engine, stop)
    if a.cmd == "recover":
        out = engine.startup()
        _print({"result": out.result if out else "none", "message_es": out.message_es if out else ""})
        if out is not None and out.result == "restart_updater":
            return RELOAD_EXIT_CODE
        return 0
    if a.cmd == "check":
        start = engine.startup()
        if start is not None and start.result == "restart_updater":
            return RELOAD_EXIT_CODE
        out = engine.check(force_window=a.force_window, apply=not a.no_apply)
        _print({"result": out.result, "message_es": out.message_es, "available": out.available})
        if out.result == "restart_updater":
            return RELOAD_EXIT_CODE
        return 1 if out.result in ("error", "update_failed") else 0
    if a.cmd == "rollback":
        engine.startup()
        out = engine.manual_rollback(a.to, a.reason)
        _print({"result": out.result, "message_es": out.message_es})
        return 0 if out.result == "rollback_ok" else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
