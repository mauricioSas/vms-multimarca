"""e2e de Windows (PLAN-V2 §4.6, pasos 1-5 y 10-12 de B3; B4 añade 6-9 en ``tests/windows/test_update_*.py``).

    py -3.12 -m tests.windows.run_e2e --installer dist\\VMSMultimarca-Setup-2.0.0.exe [--with-v1] [--work DIR]

Necesita Windows, una consola de administrador y un equipo de pruebas: **instala y desinstala de verdad** y borra
lo que encuentre de VMS Multimarca (servicios, Program Files y ProgramData). Úsalo en una VM con instantánea o en
el runner de CI, nunca en un PC con datos. Resultados en ``<artefactos>/results-windows.json`` y
``RESULTADOS-windows.md``; capturas del asistente en ``<artefactos>/capturas-asistente``.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ordered_files(base: Path = ROOT / "tests" / "windows") -> list[Path]:
    """Orden de PLAN-V2 §4.6: v1 → asistente → pasos 1-5 (B3) → 6-9 (B4, ``test_update_*.py``) → 10-12 (B3)."""
    e2e = sorted((base / "e2e").glob("test_*.py"))
    before = [p for p in e2e if p.name < "test_06"]
    after = [p for p in e2e if p.name >= "test_06"]
    return [*before, *sorted(base.glob("test_update_*.py")), *after]


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.windows.run_e2e", description=__doc__.split("\n\n")[0])
    p.add_argument("--installer", type=Path, required=True)
    p.add_argument("--work", type=Path, default=ROOT / ".tmp" / "e2e-windows")
    p.add_argument("--artifacts", type=Path)
    p.add_argument("--mode", choices=["auto", "doubles", "real"], default="auto")
    p.add_argument("--with-v1", action="store_true", help="también v1 (install.ps1) → v2; descarga ~200 MB")
    p.add_argument("--yes", action="store_true", help="no preguntar (CI): este equipo es de pruebas")
    p.add_argument("pytest_args", nargs="*")
    args = p.parse_args(argv)
    if sys.platform != "win32":
        print("El e2e de Windows solo corre en Windows.", file=sys.stderr)
        return 2
    if not _is_admin():
        print("Ejecútalo como administrador (instala servicios y reglas de firewall).", file=sys.stderr)
        return 2
    if not args.installer.is_file():
        print(f"No existe el instalador {args.installer}", file=sys.stderr)
        return 2
    if not args.yes and os.environ.get("CI") != "true":
        answer = input("Este e2e BORRA cualquier instalación de VMS Multimarca de este equipo. ¿Seguir? (si/no) ")
        if answer.strip().lower() not in ("si", "sí", "s"):
            return 1
    work = args.work.resolve()
    artifacts = (args.artifacts or work / "artefactos").resolve()
    env = dict(os.environ)
    env.update({
        "VMS_TEST_WIN_INSTALLER": str(args.installer.resolve()),
        "VMS_TEST_WIN_WORK": str(work),
        "VMS_TEST_WIN_ARTIFACTS": str(artifacts),
        "VMS_TEST_WIN_MODE": args.mode,
        "VMS_TEST_WIN_V1": "1" if args.with_v1 else "0",
        "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
    })
    artifacts.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pytest", *[str(p) for p in ordered_files()], "-p", "no:randomly",
           "-p", "no:cacheprovider", "-v", "-rs", "--timeout=2400", f"--junitxml={artifacts / 'junit-e2e.xml'}",
           *args.pytest_args]
    print("==> " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
