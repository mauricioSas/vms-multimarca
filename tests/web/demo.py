"""Entorno completo de demostración/pruebas de la interfaz: simulador de cámaras + MediaMTX real
(WebRTC, grabación y reproducción) + backend de pruebas.

    sim, engine, options = start_demo(Path(".tmp/web-demo"))
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from tests.fakes import FakeDeviceClient
from tests.web.mtx_engine import MtxTestEngine
from tests.web.stub_backend import StubOptions, fake_discover, rtsp_snapshot
from tools.camsim.simulator import CameraSimulator, SimDevice, find_ffmpeg, find_mediamtx_for_tests

ROOT = Path(__file__).resolve().parents[2]
PEOPLE_VIDEO = ROOT / "tests" / "assets" / "people-walking-h264.mp4"


def sim_devices() -> list[SimDevice]:
    videos = {1: PEOPLE_VIDEO} if PEOPLE_VIDEO.is_file() else {}
    return [SimDevice("hik1", "hikvision", channels=2, videos=videos),
            SimDevice("dah1", "dahua", channels=2)]


def device_client_for(sim: CameraSimulator) -> Any:
    """Cliente de equipo de pruebas: el simulador solo habla RTSP, así que la parte HTTP
    (modelo, canales, snapshot) la da FakeDeviceClient con el número de canales real."""
    by_port = {d.port: d for d in sim.devices.values()}

    def factory(device: Any, password: str) -> FakeDeviceClient:
        dev = by_port.get(int(getattr(device, "rtsp_port", 0)))
        if dev is None:
            return FakeDeviceClient(device.vendor, reachable=False)
        return FakeDeviceClient(device.vendor, channels=dev.channels, password_ok=password == dev.password,
                                kind="nvr" if device.kind == "nvr" else "camera")

    return factory


def start_demo(workdir: Path, *, mediamtx_bin: str | None = None, ffmpeg_bin: str | None = None,
               segment_seconds: int = 60) -> tuple[CameraSimulator, MtxTestEngine, StubOptions]:
    workdir = Path(workdir).resolve()
    mtx = mediamtx_bin or find_mediamtx_for_tests()
    sim = CameraSimulator(sim_devices(), workdir / "camsim", mediamtx_bin=mtx,
                          ffmpeg_bin=ffmpeg_bin or find_ffmpeg()).start()
    engine = MtxTestEngine(mtx, workdir / "engine", segment_seconds=segment_seconds)
    engine.start_sync()
    options = StubOptions(device_client=device_client_for(sim), discover=fake_discover,
                          snapshot=rtsp_snapshot(engine), status_interval=2.0)
    return sim, engine, options


class ServerThread:
    """Sirve una app ASGI en 127.0.0.1 dentro de un hilo, con lifespan (arranque y parada)."""

    def __init__(self, app: Any, port: int = 0) -> None:
        if port == 0:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="on", access_log=False,
                                                    timeout_graceful_shutdown=3))
        self.thread = threading.Thread(target=self.server.run, name=f"web-stub-{port}", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 20.0) -> "ServerThread":
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("El servidor de pruebas terminó al arrancar")
            if time.monotonic() > deadline:
                raise TimeoutError("El servidor de pruebas no arrancó a tiempo")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
