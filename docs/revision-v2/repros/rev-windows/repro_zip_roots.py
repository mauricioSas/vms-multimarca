"""H: los zips de componente que monta B3 (distribution/layout.py) no los acepta B4 (publish/stage):
B3 los empaqueta relativos a la carpeta del componente (vms/…, python.exe, mediamtx.exe); B4 exige las
raíces de COMPONENT_ROOTS (app/…, bin/…, runtime/…, engine/…)."""
import sys, zipfile
from pathlib import Path
REPO = Path("/Users/mauriciosas/Documents/vms-multimarca/.claude/worktrees/integracion")
sys.path[:0] = [str(REPO), str(REPO / "updater")]
from tools.release.publish import check_zip, find_component_zips, PublishError
HERE = Path(__file__).parent
out = sorted(HERE.glob("tmp*/out"))[0]
found = find_component_zips(out)
print("zips encontrados por publish:", {k: v[0].name for k, v in found.items()})
for comp, (path, _) in sorted(found.items()):
    names = [n for n in zipfile.ZipFile(path).namelist() if n != "MANIFEST.sha256"][:3]
    try:
        check_zip(comp, path); res = "ACEPTADO"
    except PublishError as e:
        res = f"RECHAZADO: {e}"
    print(f"{comp:8} {names} -> {res}")
