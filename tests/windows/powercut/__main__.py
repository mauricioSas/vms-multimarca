"""`python -m tests.windows.powercut` (ver `__init__.py`)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import STATES, HyperV, plan, report, run_one, write


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.windows.powercut")
    ap.add_argument("--vm", required=True)
    ap.add_argument("--checkpoint", required=True, help="punto de control con la 2.0.0 instalada y grabando")
    ap.add_argument("--source", required=True, help="VMS_UPDATE_SOURCE del repositorio de prueba (con la 2.0.1)")
    ap.add_argument("--target", default="2.0.1")
    ap.add_argument("--from-version", default="2.0.0")
    ap.add_argument("--states", default="all")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--wait-min", type=float, default=10)
    ap.add_argument("--credential", help="Export-Clixml del usuario administrador de la VM")
    ap.add_argument("--out", type=Path, default=Path("tests/e2e/RESULTADOS-powercut.json"))
    ap.add_argument("--dry-run", action="store_true", help="solo muestra el plan")
    a = ap.parse_args(argv)
    states = list(STATES) if a.states == "all" else a.states.split(",")
    steps = plan(states, a.repeat)
    print(f"{len(steps)} cortes: {', '.join(f'{s}#{i}' for s, i in steps)}")
    if a.dry_run:
        return 0
    if sys.platform != "win32":
        print("Solo en el anfitrión Windows con Hyper-V", file=sys.stderr)
        return 2
    vm = HyperV(a.vm, a.credential)
    results = []
    for state, i in steps:
        o = run_one(vm, checkpoint=a.checkpoint, state=state, repeat=i, source=a.source, wait_min=a.wait_min,
                    from_version=a.from_version, to_version=a.target)
        print(f"{state}#{i}: {o.verdict} {'; '.join(o.reasons)}")
        results.append(o)
        write(report(results), a.out)            # se guarda tras cada corte: un fallo a mitad no pierde nada
    rep = report(results)
    write(rep, a.out)
    print(f"{rep['ok']}/{rep['total']} bien. Informe: {a.out} y {a.out.with_suffix('.md')}")
    return 0 if rep["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
