"""E2E: un muro en kiosco sigue con vídeo tras reiniciar el backend (caída, actualización o reinicio
del servicio). Antes, las sesiones solo vivían en memoria y el muro acababa en /login hasta que el
lanzador del kiosco lo reabría (6 h por defecto): monitores de vigilancia sin vídeo.

Procesos reales: simulador RTSP + `python -m vms` (con su MediaMTX) + Chromium.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx
import pytest

from tests.e2e.system_check import CELLS_JS
from tools.dev_run import (BackendClient, Service, base_env, free_port, python_cmd, seed_backend, start_simulator,
                           wait_until, write_env_file)

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg,
              pytest.mark.needs_browser]

KIOSK = "kiosco-reinicio-" + os.urandom(6).hex()
ADMIN = "Admin#Reinicio-26"


@pytest.fixture
def stack(tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str) -> Iterator[dict[str, Any]]:
    simenv = start_simulator(tmp_path / "sim", hik_channels=2, dah_channels=1, people_cameras=0)
    port = free_port()
    values = {
        "VMS_DATA_DIR": str(tmp_path / "data"), "VMS_SITE_ID": "site-kiosk-001", "VMS_HTTP_HOST": "127.0.0.1",
        "VMS_HTTP_PORT": str(port), "VMS_ADMIN_INITIAL_PASSWORD": ADMIN, "VMS_KIOSK_TOKEN": KIOSK,
        "VMS_CREDENTIAL_BACKEND": "file", "VMS_LLM_PROVIDER": "none", "VMS_MEDIAMTX_BIN": mediamtx_bin,
        "VMS_MTX_RTSP_ADDRESS": f"127.0.0.1:{free_port()}", "VMS_MTX_WEBRTC_ADDRESS": f"127.0.0.1:{free_port()}",
        "VMS_MTX_WEBRTC_ICE_UDP": f":{free_port()}", "VMS_MTX_WEBRTC_ICE_TCP": "off",
        "VMS_MTX_API_ADDRESS": f"127.0.0.1:{free_port()}", "VMS_MTX_PLAYBACK_ADDRESS": f"127.0.0.1:{free_port()}",
        "VMS_MTX_METRICS_ADDRESS": f"127.0.0.1:{free_port()}",
    }
    env_file = tmp_path / "kiosk.env"
    write_env_file(env_file, values)
    env = base_env(env_file)
    base = f"http://127.0.0.1:{port}"
    state: dict[str, Any] = {"base": base, "env": env, "log": tmp_path / "backend.log", "svc": None}

    def start() -> Service:
        svc = Service("backend", python_cmd("-m", "vms"), env, state["log"]).start()
        wait_until(lambda: httpx.get(f"{base}/api/health", timeout=2).json()["engine"]["running"], 60,
                   "backend con motor en marcha")
        state["svc"] = svc
        return svc

    state["start"] = start
    start()
    try:
        api = BackendClient(base, "admin", ADMIN)
        seed_backend(api, simenv)
        api.close()
        yield state
    finally:
        if state["svc"] is not None:
            state["svc"].stop()
        simenv.stop()


def _wall_playing(page: Any, gap: float = 2.0) -> bool:
    a = {c["index"]: c for c in page.evaluate(CELLS_JS)}
    time.sleep(gap)
    cells = page.evaluate(CELLS_JS)
    return bool(cells) and all(c["w"] > 0 and c["state"] == "live" and c["t"] - a.get(c["index"], c)["t"] > gap * 0.4
                               for c in cells)


def test_kiosk_wall_keeps_video_after_backend_restart(stack: dict[str, Any]) -> None:
    from playwright.sync_api import sync_playwright

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    base = stack["base"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        try:
            page = browser.new_page(base_url=base)
            page.goto(f"/api/auth/kiosk?token={quote(KIOSK)}&next=/wall/1")
            page.wait_for_url("**/wall/1")
            page.wait_for_function("() => window.__vmsWall && window.__vmsWall.cells().length > 0", timeout=20000)
            wait_until(lambda: _wall_playing(page), 60, "muro con vídeo antes del reinicio")

            stack["svc"].stop()                       # SIGTERM, como WinSW/systemd al reiniciar
            time.sleep(3)
            stack["start"]()

            status = page.evaluate("fetch('/api/auth/me', {headers: {'X-Requested-With': 'vms'}}).then(r => r.status)")
            assert status == 200, "la cookie del kiosco no vale tras el reinicio"
            took = wait_until(lambda: _wall_playing(page), 90, "muro con vídeo después del reinicio")
            time.sleep(5)
            assert "/wall/1" in page.url and "/login" not in page.url
            assert _wall_playing(page)
            print(f"Muro con vídeo de nuevo {took:.1f} s después de volver el backend")
        finally:
            browser.close()
