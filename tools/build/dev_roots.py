"""``python -m tools.build dev-roots --out DIR``: roots de confianza de DESARROLLO para una build de prueba.

Crea un llavero ``dev`` desechable (en una carpeta temporal, nunca en el repositorio) y los repositorios TUF online y
offline con ``tools.release``, y deja ``DIR/online/1.root.json`` y ``DIR/offline/1.root.json``. Con
``VMS_BUILD_TRUSTED_ROOTS=DIR`` el payload lleva esos roots y el instalador acepta ``UpdateSource`` (sin root, el
actualizador no podría verificar nada y el instalador la rechaza). Solo para CI y pruebas: los de producción salen
de la ceremonia de llaves (``docs/PUBLICAR-VERSION.md``) y van en ``distribution/trusted/``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from distribution.layout import TRUSTED_MODES

from . import ROOT
from .inno import BuildError


def make_dev_roots(out: Path) -> dict[str, str]:
    """Roots ``dev`` en ``out/<modo>/1.root.json``. Devuelve modo → ruta."""
    with tempfile.TemporaryDirectory(prefix="vms-dev-roots-") as tmp:
        base = [sys.executable, "-m", "tools.release", "--keys", str(Path(tmp) / "keys"), "--repo",
                str(Path(tmp) / "repo")]
        for cmd in (["keys", "init-dev"], ["init"]):
            proc = subprocess.run([*base, *cmd], cwd=ROOT, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise BuildError(f"tools.release {' '.join(cmd)} falló: {proc.stderr.strip()[-500:]}")
        made: dict[str, str] = {}
        for mode in TRUSTED_MODES:
            src = Path(tmp) / "repo" / mode / "metadata" / "1.root.json"
            dst = out / mode / "1.root.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            made[mode] = str(dst)
    return made


def main(out: str) -> int:
    print(json.dumps(make_dev_roots(Path(out)), ensure_ascii=False, indent=2))
    return 0
