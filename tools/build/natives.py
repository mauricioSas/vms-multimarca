"""Binarios nativos del payload: los de producto (B1/B2) o los dobles de prueba de B3.

- Producto: ``vmshost.exe`` y ``vmsctl.exe`` desde ``native/`` (B1) y ``VMS.exe`` desde ``native/viewer`` (B2).
- Dobles (``--doubles``): ``tests/windows/doubles`` compila un ``vmsctl.exe`` que **registra cada llamada**
  (y simula fallos a petición), un ``vmshost.exe`` que solo sabe abrir el visor y un ``VMS.exe`` que solo
  abre una ventana. Sirven para probar el instalador mientras B1 y B2 no entregan (PLAN-V2 §6.2, B3).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import DOUBLES_DIR, NATIVE_DIR
from .inno import BuildError

EXE = ".exe" if sys.platform == "win32" else ""


@dataclass(frozen=True)
class NativeArtifacts:
    vmshost: Path
    vmsctl: Path
    viewer: Path
    doubles: bool


def find_cargo() -> str:
    found = shutil.which("cargo")
    if found:
        return found
    home = Path.home() / ".cargo" / "bin" / f"cargo{EXE}"
    if home.is_file():
        return str(home)
    raise BuildError("No encuentro cargo (Rust). Instálalo con rustup: https://rustup.rs")


def _cargo(args: list[str], cwd: Path) -> None:
    env = dict(os.environ)
    env.setdefault("CARGO_TERM_COLOR", "never")
    cargo = find_cargo()
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")   # rustup necesita su carpeta
    proc = subprocess.run([cargo, *args], cwd=cwd, env=env, timeout=3600, check=False)
    if proc.returncode != 0:
        raise BuildError(f"cargo {' '.join(args)} falló en {cwd} (código {proc.returncode})")


def build_doubles(release: bool = True) -> NativeArtifacts:
    profile = "release" if release else "debug"
    _cargo(["build", "--locked", *(["--release"] if release else [])], DOUBLES_DIR)
    target = DOUBLES_DIR / "target" / profile
    arts = NativeArtifacts(target / f"vmshost{EXE}", target / f"vmsctl{EXE}", target / f"VMS{EXE}", True)
    _check(arts)
    return arts


def build_product(viewer: Path | None = None) -> NativeArtifacts:
    _cargo(["build", "--release", "--locked", "-p", "vmshost", "-p", "vmsctl"], NATIVE_DIR)
    target = NATIVE_DIR / "target" / "release"
    if viewer is None:
        viewer = NATIVE_DIR / "viewer" / "src-tauri" / "target" / "release" / f"VMS{EXE}"
    arts = NativeArtifacts(target / f"vmshost{EXE}", target / f"vmsctl{EXE}", viewer, False)
    _check(arts)
    return arts


def _check(arts: NativeArtifacts) -> None:
    for label, path in (("vmshost", arts.vmshost), ("vmsctl", arts.vmsctl), ("visor", arts.viewer)):
        if not path.is_file():
            hint = " (el visor lo entrega B2; mientras tanto, usa --doubles)" if label == "visor" else ""
            raise BuildError(f"No se generó el binario {label}: {path}{hint}")


def is_double(vmsctl: Path) -> bool:
    """El doble se identifica en ``--version`` («vmsctl-doble»)."""
    try:
        out = subprocess.run([str(vmsctl), "--version"], capture_output=True, text=True, timeout=30, check=False)
    except OSError:
        return False
    return "doble" in out.stdout
