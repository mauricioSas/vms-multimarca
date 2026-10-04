"""Valida las premisas de la arquitectura con procesos reales (sin hardware):

  equipo simulado (rutas Hikvision/Dahua nativas + Digest) → MediaMTX (rutas añadidas por API,
  sin credenciales en el YAML) → lectores (ffprobe)

- Una sola conexión al equipo por flujo aunque haya varios lectores.
- El subflujo bajo demanda solo abre conexión cuando alguien lo lee.
- MediaMTX se reconecta solo cuando el flujo vuelve (matar/relanzar) y cuando el equipo vuelve.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

import httpx
import pytest

from tests.conftest import get_free_port
from tools.camsim.simulator import CameraSimulator, SimDevice

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg]


class MiniMtx:
    """MediaMTX mínimo para validar infraestructura (el motor real lo implementa vms.engine)."""

    def __init__(self, exe: str, workdir: Path) -> None:
        self.rtsp_port, self.api_port = get_free_port(), get_free_port()
        workdir.mkdir(parents=True, exist_ok=True)
        cfg = workdir / "mediamtx.yml"
        cfg.write_text(
            "logLevel: info\nlogDestinations: [stdout]\n"
            "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\nmoq: no\nplayback: no\nmetrics: no\n"
            f"api: yes\napiAddress: 127.0.0.1:{self.api_port}\n"
            f"rtsp: yes\nrtspTransports: [tcp]\nrtspAddress: 127.0.0.1:{self.rtsp_port}\n"
            "authInternalUsers:\n  - user: any\n    pass:\n    ips: ['127.0.0.1', '::1']\n"
            "    permissions:\n      - action: read\n      - action: api\n"
            "pathDefaults:\n  rtspTransport: tcp\n"
            "paths: {}\n", encoding="utf-8")
        self.log = workdir / "mediamtx.log"
        with open(self.log, "wb") as fh:
            self.proc = subprocess.Popen([exe, str(cfg)], cwd=workdir, stdout=fh, stderr=subprocess.STDOUT)
        self.api = httpx.Client(base_url=f"http://127.0.0.1:{self.api_port}", timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.api.get("/v3/paths/list").raise_for_status()
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise TimeoutError("MediaMTX no arrancó")

    def add_path(self, name: str, conf: dict[str, Any]) -> None:
        r = self.api.post(f"/v3/config/paths/add/{name}", json=conf)
        assert r.status_code == 200, r.text

    def path(self, name: str) -> dict[str, Any] | None:
        r = self.api.get(f"/v3/paths/get/{name}")
        return r.json() if r.status_code == 200 else None

    def wait_ready(self, name: str, ready: bool = True, timeout: float = 20.0) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            info = self.path(name)
            if bool(info and info.get("ready")) == ready:
                return time.monotonic() - t0
            time.sleep(0.2)
        raise TimeoutError(f"{name} no quedó ready={ready} en {timeout} s; log: {self.log}")

    def stop(self) -> None:
        self.api.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def mini_mtx(mediamtx_bin: str, tmp_path: Path) -> Iterator[MiniMtx]:
    m = MiniMtx(mediamtx_bin, tmp_path / "mtx")
    yield m
    m.stop()


def probe(ffprobe: str, url: str) -> str:
    r = subprocess.run([ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", url],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or r.stderr.strip()


def test_simulator_streams_and_auth(camsim: CameraSimulator, ffprobe_bin: str) -> None:
    hik, dah = camsim.device("hik1"), camsim.device("dah1")
    assert probe(ffprobe_bin, hik.rtsp_url(2, "main")) == "h264,640,360"
    assert probe(ffprobe_bin, dah.rtsp_url(1, "sub")) == "h264,320,180"
    assert "401" in probe(ffprobe_bin, dah.rtsp_url(1, "main").replace("Sim%23Pass", "Mala"))
    assert "404" in probe(ffprobe_bin, hik.rtsp_url(1, "main").replace("/101", "/901"))


def test_mediamtx_pulls_simulated_devices_one_connection_per_stream(
        camsim_factory: Callable[..., CameraSimulator], mini_mtx: MiniMtx, ffprobe_bin: str) -> None:
    sim = camsim_factory([SimDevice("hik1", "hikvision", channels=1), SimDevice("dah1", "dahua", channels=2)])
    hik, dah = sim.device("hik1"), sim.device("dah1")
    mini_mtx.add_path("cam-hik00001/main", {"source": hik.rtsp_url(1, "main")})
    mini_mtx.add_path("cam-hik00001/sub", {"source": hik.rtsp_url(1, "sub"), "sourceOnDemand": True})
    mini_mtx.add_path("cam-dah00002/main", {"source": dah.rtsp_url(2, "main")})

    mini_mtx.wait_ready("cam-hik00001/main")
    mini_mtx.wait_ready("cam-dah00002/main")
    assert sim.proxy_connections("hik1") == 1
    assert sim.proxy_connections("dah1") == 1

    base = f"rtsp://127.0.0.1:{mini_mtx.rtsp_port}"
    assert probe(ffprobe_bin, f"{base}/cam-hik00001/main") == "h264,640,360"
    assert probe(ffprobe_bin, f"{base}/cam-hik00001/main") == "h264,640,360"
    assert probe(ffprobe_bin, f"{base}/cam-dah00002/main") == "h264,640,360"
    assert sim.proxy_connections("hik1") == 1, "dos lectores no deben abrir más conexiones al equipo"

    assert probe(ffprobe_bin, f"{base}/cam-hik00001/sub") == "h264,320,180"
    assert sim.proxy_connections("hik1") == 2, "el subflujo bajo demanda abre exactamente una conexión"

    conf = mini_mtx.api.get(f"/v3/config/paths/get/{quote('cam-hik00001/main', safe='/')}").json()
    assert "source" in conf  # la API sí expone la URL: por eso solo escucha en 127.0.0.1


def test_mediamtx_reconnects_after_stream_and_device_loss(
        camsim_factory: Callable[..., CameraSimulator], mini_mtx: MiniMtx) -> None:
    sim = camsim_factory([SimDevice("hik1", "hikvision", channels=1)])
    hik = sim.device("hik1")
    mini_mtx.add_path("cam-hik00001/main", {"source": hik.rtsp_url(1, "main")})
    mini_mtx.wait_ready("cam-hik00001/main")

    sim.kill_stream("hik1", 1, "main")
    mini_mtx.wait_ready("cam-hik00001/main", ready=False, timeout=20)
    sim.start_stream("hik1", 1, "main")
    recovered = mini_mtx.wait_ready("cam-hik00001/main", timeout=30)

    sim.set_device_online("hik1", False)
    mini_mtx.wait_ready("cam-hik00001/main", ready=False, timeout=20)
    sim.set_device_online("hik1", True)
    recovered_dev = mini_mtx.wait_ready("cam-hik00001/main", timeout=30)
    print(f"reconexión tras flujo: {recovered:.1f} s; tras equipo: {recovered_dev:.1f} s")


def test_simulator_channel_from_people_video(camsim_factory: Callable[..., CameraSimulator], people_video: Path,
                                             ffprobe_bin: str) -> None:
    """Canal 1 del NVR simulado = vídeo real de personas (para la analítica), canal 2 = patrón."""
    sim = camsim_factory([SimDevice("hik1", "hikvision", channels=2, videos={1: people_video},
                                    main_size=(1280, 720), sub_size=(640, 360))])
    assert probe(ffprobe_bin, sim.device("hik1").rtsp_url(1, "sub")) == "h264,640,360"
    assert probe(ffprobe_bin, sim.device("hik1").rtsp_url(1, "main")) == "h264,1280,720"
