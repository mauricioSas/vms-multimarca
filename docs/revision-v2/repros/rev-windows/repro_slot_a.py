"""H1: la ranura A que monta B3 (distribution/layout.py) no tiene runtime ni app/: vmsctl run --service VMSUpdater
(B1) no encuentra Python. Se monta el payload real y se ejecuta el vmsctl de verdad (build nativo macOS) desde
updater/slot-a."""
import os, shutil, subprocess, sys, tempfile, json
from pathlib import Path
REPO = Path("/Users/mauriciosas/Documents/vms-multimarca/.claude/worktrees/integracion")
sys.path.insert(0, str(REPO))
from distribution.layout import LayoutInputs, build_layout
VMSCTL = Path(__file__).parent / "target" / "debug" / "vmsctl"

t = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
bins = t / "bins"; bins.mkdir()
for n in ("vmshost.exe", "vmsctl.exe", "VMS.exe"):
    (bins / n).write_bytes(b"MZ" + n.encode())
eng = t / "engine"; eng.mkdir(); (eng / "mediamtx.exe").write_bytes(b"MZ"); (eng / "LICENSE").write_text("MIT\n")
rt = t / "runtime"; (rt / "Lib" / "site-packages").mkdir(parents=True); (rt / "python.exe").write_bytes(b"MZ")
(rt / "python312._pth").write_text("python312.zip\n.\nLib\\site-packages\n..\\app\nimport site\n")
(rt / "VERSION").write_text("3.12.10-r1\n")
res = build_layout(LayoutInputs(version="2.0.0", vmshost=bins / "vmshost.exe", vmsctl=bins / "vmsctl.exe",
                                viewer=bins / "VMS.exe", engine_dir=eng, runtime_dir=rt, epoch=1759622400,
                                repo=REPO), t / "out")
slot = res.payload / "updater" / "slot-a"
print("contenido de payload/updater/slot-a (nivel 1):", sorted(p.name for p in slot.iterdir()))
print("¿slot-a/runtime?", (slot / "runtime").exists(), " ¿slot-a/app?", (slot / "app").exists())
# Lo que espera B1 (native/ci/b1_windows_e2e.py:183-188): slot-a/{vmsctl.exe, runtime/, app/vms_updater}
# Ahora ejecutamos el vmsctl real desde esa ranura (en macOS: updater/slot-a/vmsctl)
shutil.copy2(VMSCTL, slot / "vmsctl")
data = t / "data"; data.mkdir()
p = subprocess.run([str(slot / "vmsctl"), "run", "--service", "VMSUpdater", "--data-dir", str(data),
                    "--exit-on-crash", "--json"], capture_output=True, text=True, timeout=30)
print("vmsctl run --service VMSUpdater -> código", p.returncode)
print("stdout:", p.stdout.strip()[:400]); print("stderr:", p.stderr.strip()[:400])
log = data / "logs" / "VMSUpdater.log"
if log.exists():
    print("logs/VMSUpdater.log:\n" + log.read_text()[-600:])
st = data / "logs" / "status-VMSUpdater.json"
if st.exists(): print("status:", st.read_text()[:300])
