"""H5: una actualización solo de `app` (VMSEngine NO está en `restart`) reinicia igualmente el motor.

El vmshost REAL de VMSEngine (B1) vigila active.json y, en cuanto el actualizador (B4, PointerStore.switch,
el paso `switched`) cambia la versión activa, para su hijo y lanza versions/<nueva>/bin/vmsctl. Los «vmsctl» son
guiones que anotan cuándo arrancan y cuándo paran (al cerrarse su entrada estándar, como hace vmsctl run)."""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).parent
REPO = Path("/Users/mauriciosas/Documents/vms-multimarca/.claude/worktrees/integracion")
sys.path.insert(0, str(REPO / "updater"))
from vms_updater.pointer import PointerStore
from vms_updater.journal import JournalStore
from vms_updater.models import ActivePointer, Journal

t = Path(tempfile.mkdtemp(dir=HERE)); pf, pd = t / "pf", t / "pd"
events = t / "events.log"
(pf / "bin").mkdir(parents=True)
(pf / "bin" / "vmshost").write_bytes((HERE / "target/debug/vmshost").read_bytes()); os.chmod(pf / "bin/vmshost", 0o755)
for v in ("2.0.0", "2.1.0"):
    b = pf / "versions" / v / "bin"; b.mkdir(parents=True)
    (b / "vmsctl").write_text(f"#!/bin/sh\necho \"$(date +%s.%N) arranca motor {v} ($*)\" >> {events}\n"
                              f"while read l; do :; done\necho \"$(date +%s.%N) PARA motor {v}\" >> {events}\n")
    os.chmod(b / "vmsctl", 0o755)
L = pd / "state"; L.mkdir(parents=True)
PointerStore(L / "active.json").write(ActivePointer(active="2.0.0"))
JournalStore(L / "journal.json").write(Journal(state="good", last_good="2.0.0"))
host = subprocess.Popen([str(pf / "bin/vmshost"), "service", "--name", "VMSEngine", "--foreground",
                         "--install-dir", str(pf), "--data-dir", str(pd)], stdin=subprocess.PIPE)
time.sleep(2)
# Paso `switched` del actualizador para una versión cuyo único cambio es `app` (restart = Backend/Analytics/...):
desc_restart_app = ["VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral"]   # distribution/layout.py RESTART
print("servicios que el actualizador para/arranca para «app»:", desc_restart_app, "-> VMSEngine NO")
PointerStore(L / "active.json").switch("2.1.0", trial=True)
time.sleep(3)
host.stdin.close(); host.wait(timeout=30)
print(events.read_text())
print("vmshost-VMSEngine.log:\n" + (pd / "logs" / "vmshost-VMSEngine.log").read_text())
