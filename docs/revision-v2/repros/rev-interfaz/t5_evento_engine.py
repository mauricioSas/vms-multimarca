"""¿Quién conecta MediaMtxEngine.on_event con publish_engine (CONTRATO §17.3) en la app real?"""
import tempfile
from pathlib import Path
from common import *
from fastapi.testclient import TestClient
from vms.api.app import create_app
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from tests.conftest import get_free_port

base = Path(tempfile.mkdtemp(dir=SCRATCH / "tmp"))
paths = AppPaths(base).ensure()
s = VmsSettings(_env_file=None, data_dir=base, site_id="site-test", http_host="127.0.0.1", http_port=get_free_port(),
                mtx_api_address=f"127.0.0.1:{get_free_port()}", internal_token="x" * 32, kiosk_token="k" * 32,
                credential_backend="file", mediamtx_bin=REPO / "bin" / "mediamtx", engine_mode="attach")
app = create_app(s, start_engine=False, heartbeat=False)
with TestClient(app) as c:
    eng = app.state.vms.engine
    print("motor:", type(eng).__name__, "modo:", eng.mode)
    print("engine.on_event tras el arranque de la app:", eng.on_event)
    eng._emit("restarted", 1234)   # lo que hace el motor al detectar un reinicio
    print("suscriptores del bus:", app.state.vms.bus.subscribers, "| el evento no llega a ningún sitio")
