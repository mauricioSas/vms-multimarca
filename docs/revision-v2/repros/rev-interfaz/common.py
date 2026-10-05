"""Arnés común: backend REAL (tests.web.real_backend) + Playwright. Solo lectura del repo."""
from __future__ import annotations
import os, sys, tempfile, time
from pathlib import Path
REPO = Path("/Users/mauriciosas/Documents/vms-multimarca/.claude/worktrees/integracion")
sys.path.insert(0, str(REPO))
for k in [k for k in os.environ if k.startswith("VMS_")]:
    del os.environ[k]
os.environ["VMS_ENV_FILE"] = str(Path(tempfile.gettempdir()) / "no-existe.env")
os.environ["PYTHON_KEYRING_BACKEND"] = "vms.core.credentials.MemoryKeyring"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
import httpx  # noqa: E402
from tests.fakes import FakeEngine  # noqa: E402
from tests.web.demo import ServerThread  # noqa: E402
from tests.web.real_backend import RealBackend  # noqa: E402
from tests.web.stub_backend import DEFAULT_USERS  # noqa: E402

ADMIN = ("admin", DEFAULT_USERS["admin"][0])
OPER = ("operador", DEFAULT_USERS["operador"][0])
SCRATCH = Path(__file__).parent


def start(engine=None):
    d = Path(tempfile.mkdtemp(prefix="rev-", dir=SCRATCH / "tmp"))
    be = RealBackend(d / "datos", engine or FakeEngine())
    srv = ServerThread(be.app).start()
    return be, srv


def client(srv, user=ADMIN):
    c = httpx.Client(base_url=srv.base_url, headers={"X-Requested-With": "vms"}, timeout=10)
    c.post("/api/auth/login", json={"username": user[0], "password": user[1]}).raise_for_status()
    return c


def setup_scope(srv):
    """Admin crea un NVR con 2 cámaras; el operador solo tiene la c1 (vivo)."""
    a = client(srv)
    r = a.post("/api/devices", json={"name": "NVR Tienda", "vendor": "hikvision", "kind": "nvr", "host": "10.0.0.5",
                                     "username": "admin", "password": "Cl@ve#1", "import_channels": [1, 2]})
    assert r.status_code == 201, r.text
    c1, c2 = r.json()["cameras"]
    r = a.patch("/api/users/operador", json={"camera_scope": {"cameras": [c1], "live": True, "playback": True,
                                                              "export": False}})
    assert r.status_code == 200, r.text
    time.sleep(0.5)
    return a, c1, c2


def browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    b = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    return pw, b
