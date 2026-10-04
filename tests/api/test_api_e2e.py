"""Extremo a extremo por la API, con todo real salvo el hardware:

  API FastAPI → clientes ISAPI/CGI reales → mocks HTTP de Hikvision y Dahua (Digest)
              → motor MediaMTX real → simulador RTSP con rutas nativas Hik/Dahua (Digest)

Alta de 2 equipos (estilo Hikvision y estilo Dahua) importando sus canales, vídeo y grabación,
línea de tiempo (/list), vídeo grabado en MP4 válido según ffprobe, reconexión al matar y
relanzar un flujo, snapshot del equipo, proxy WHEP hasta el MediaMTX real, y que ninguna
contraseña aparezca en config.json, mediamtx.yml ni en los registros.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
import pytest
from pydantic import SecretStr

from tests.api.conftest import ADMIN_PW, HEADERS
from tools.camsim.simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice
from tools.mocks.dahua import DahuaChannel, DahuaMock
from tools.mocks.hikvision import HikChannel, HikvisionMock
from vms.api import create_app
from vms.core.credentials import CredentialStore
from vms.core.settings import VmsSettings

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg]


async def poll(check: Callable[[], Awaitable[bool]], timeout: float, what: str) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await check():
            return time.monotonic() - t0
        await asyncio.sleep(0.3)
    raise AssertionError(f"Tiempo agotado esperando: {what}")


@pytest.fixture
async def stack(settings: VmsSettings, credential_store: CredentialStore, mediamtx_bin: str,
                camsim_factory: Callable[..., CameraSimulator], mock_server: Callable[[Any], Any]
                ) -> AsyncIterator[dict[str, Any]]:
    sim = camsim_factory([SimDevice("hik1", "hikvision", channels=1), SimDevice("dah1", "dahua", channels=1)])
    hik_api = mock_server(HikvisionMock(password=DEFAULT_PASSWORD, channels=[HikChannel("Entrada")]).app)
    dah_api = mock_server(DahuaMock(password=DEFAULT_PASSWORD, channels=[DahuaChannel("Cajas")]).app)
    s = settings.model_copy(update={"admin_initial_password": SecretStr(ADMIN_PW), "mediamtx_bin": Path(mediamtx_bin)})
    app = create_app(s, credential_store=credential_store, heartbeat=False, apply_delay=0.2)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                   headers=HEADERS, timeout=30)
        r = await client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200
        try:
            yield {"app": app, "c": client, "sim": sim, "hik_api": hik_api, "dah_api": dah_api, "settings": s}
        finally:
            await client.aclose()


async def test_full_stack_two_vendors(stack: dict[str, Any], ffprobe_bin: str, tmp_path: Path,
                                      caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)  # también los registros de depuración deben estar limpios
    c: httpx.AsyncClient = stack["c"]
    sim: CameraSimulator = stack["sim"]

    # 1. Alta de los dos equipos importando sus canales por la API del fabricante
    created = {}
    for vendor, api_srv, simdev in (("hikvision", stack["hik_api"], "hik1"), ("dahua", stack["dah_api"], "dah1")):
        r = await c.post("/api/devices", json={
            "name": f"NVR {vendor}", "vendor": vendor, "kind": "nvr", "host": "127.0.0.1",
            "http_port": api_srv.port, "rtsp_port": sim.device(simdev).port, "username": "admin",
            "password": DEFAULT_PASSWORD, "import_channels": "all"})
        assert r.status_code == 201, r.text
        dev = r.json()
        assert "details" not in dev, dev.get("details")
        assert len(dev["cameras"]) == 1 and dev["has_password"]
        created[vendor] = dev
    assert created["hikvision"]["model"] == "DS-7608NI-K2/8P"
    assert created["dahua"]["model"] == "DHI-NVR4208-8P-4KS2/L"
    cams = {c_["device_id"]: c_ for c_ in (await c.get("/api/cameras")).json()}
    hik_cam = cams[created["hikvision"]["id"]]["id"]
    dah_cam = cams[created["dahua"]["id"]]["id"]
    assert cams[created["hikvision"]["id"]]["name"] == "Entrada" and cams[created["dahua"]["id"]]["name"] == "Cajas"

    # 2. MediaMTX recibe las cámaras y graba
    async def online(cam: str, value: bool = True) -> bool:
        live = (await c.get(f"/api/cameras/{cam}")).json()["live"]
        return live["online"] is value and (live["recording"] is value)

    for cam in (hik_cam, dah_cam):
        await poll(lambda cam=cam: online(cam), 30, f"vídeo de {cam}")
    st = (await c.get("/api/status")).json()
    assert st["status"] == "ok" and st["engine"]["running"] and all(x["online"] for x in st["cameras"])
    assert (await c.get("/api/health")).json()["status"] == "ok"

    # 3. Línea de tiempo y vídeo grabado (MP4 válido)
    for cam in (hik_cam, dah_cam):
        async def enough(cam: str = cam) -> bool:
            spans = (await c.get(f"/api/recordings/{cam}/timeline")).json()["spans"]
            return sum(s["duration"] for s in spans) >= 3

        await poll(enough, 30, f"tramos grabados de {cam}")
        spans = (await c.get(f"/api/recordings/{cam}/timeline")).json()["spans"]
        assert spans[0]["start"].endswith("Z")
        r = await c.get(f"/api/recordings/{cam}/video", params={"start": spans[0]["start"], "duration": 2,
                                                                "format": "mp4", "download": 1})
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4", r.text[:300]
        assert r.headers["content-disposition"].startswith("attachment;")
        out = tmp_path / f"{cam}.mp4"
        out.write_bytes(r.content)
        probe = subprocess.run([ffprobe_bin, "-v", "error", "-show_entries", "format=duration:stream=codec_name,width",
                                "-of", "json", str(out)], capture_output=True, text=True, timeout=30)
        info = json.loads(probe.stdout)
        assert info["streams"][0]["codec_name"] == "h264" and info["streams"][0]["width"] == 640
        assert 1.5 <= float(info["format"]["duration"]) <= 2.5
    summary = (await c.get("/api/recordings/summary")).json()
    assert all(x["bytes"] > 0 and x["first"] for x in summary)

    # 4. Reconexión: se mata el flujo principal del NVR Hikvision simulado y se relanza
    sim.kill_stream("hik1", 1, "main")
    await poll(lambda: online(hik_cam, False), 30, "detectar el corte")
    st = (await c.get("/api/status")).json()
    assert st["status"] == "degraded" and "cámaras sin vídeo" in st["problems"]
    sim.start_stream("hik1", 1, "main")
    took = await poll(lambda: online(hik_cam), 40, "reconexión")
    print(f"Reconexión vista por la API en {took:.1f} s")

    # 5. Snapshot del equipo real (ISAPI) en memoria
    r = await c.get(f"/api/cameras/{hik_cam}/snapshot")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"

    # 6. Proxy WHEP hasta el MediaMTX real (OPTIONS + oferta inválida → error controlado)
    r = await c.options(f"/api/live/{dah_cam}/sub/whep")
    assert r.status_code == 204
    r = await c.post(f"/api/live/{dah_cam}/sub/whep", content=b"v=0\r\n", headers={"Content-Type": "application/sdp"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "whep_rejected"

    # 7. Prueba de conexión con la contraseña guardada
    r = await c.post(f"/api/devices/{created['dahua']['id']}/test")
    assert r.json()["ok"] is True and r.json()["rtsp_ok"] is True, r.json()["message"]

    # 8. Ninguna contraseña en disco ni en los registros
    s: VmsSettings = stack["settings"]
    data = s.paths
    for f in (data.config_file, data.mediamtx_dir / "mediamtx.yml"):
        text = f.read_text(encoding="utf-8")
        assert "Sim#Pass" not in text and "Sim%23Pass" not in text, f
    assert "Sim#Pass" not in caplog.text and "Sim%23Pass" not in caplog.text

    # 9. Borrar un equipo retira sus rutas del motor
    assert (await c.delete(f"/api/devices/{created['dahua']['id']}")).status_code == 204

    async def gone() -> bool:
        st = (await c.get("/api/status")).json()
        return [x["camera_id"] for x in st["cameras"]] == [hik_cam]

    await poll(gone, 10, "baja del equipo")
