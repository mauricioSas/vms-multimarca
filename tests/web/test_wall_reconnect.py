"""Muros con vídeo real ante caídas (PLAN-V2 §6.2 B2, criterio 2; §5 pendiente 4a):

- motor (MediaMTX) matado con SIGKILL y relanzado → TODAS las celdas de dos muros vuelven a tener vídeo en
  **≤ 6 s** desde el corte (con el evento SSE `engine`, que emitirá B1 en modo attach), y sin ese evento, solo con
  la detección del propio muro, en ≤ 12 s;
- backend reiniciado (el motor sigue, modo attach) → los muros siguen en `/wall/N`, sin login y sin cortar el vídeo;
- los muros comparten UNA conexión SSE (SharedWorker), y entran con el intercambio del token de kiosco del visor
  (`POST /api/local/kiosk-session` desde la propia página), no con el token en la URL.

Procesos reales: simulador RTSP (ffmpeg) + MediaMTX + backend real (`vms.api`) + Chromium.
"""
from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from tests.conftest import get_free_port
from tests.web.conftest import ADMIN, PageErrors
from tests.web.demo import ServerThread, device_client_for
from tests.web.mtx_engine import MtxTestEngine
from tests.web.real_backend import RealBackend
from tests.web.stub_backend import StubOptions, fake_discover
from tools.camsim.simulator import CameraSimulator, SimDevice
from vms.api.events import publish_engine

pytestmark = [pytest.mark.needs_browser, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg, pytest.mark.timeout(420)]

TARGET_S = 6.0          # criterio del plan: vídeo de nuevo en ≤ 6 s tras la caída del motor
FALLBACK_S = 12.0       # sin el evento «engine» (solo lo que detecta el propio muro)

CELLS_JS = """() => [...document.querySelectorAll('.cell')].filter(c => !c.classList.contains('empty')).map(c => {
  const v = c.querySelector('video');
  const q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : {totalVideoFrames: 0};
  return {i: Number(c.dataset.index), state: c.dataset.state, w: v.videoWidth, t: v.currentTime,
          frames: q.totalVideoFrames};
})"""


class AttachedEngine:
    """MediaMTX como servicio aparte (modo attach de B1): el backend no lo arranca ni lo para."""

    def __init__(self, inner: MtxTestEngine) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def start(self) -> None:
        if self.inner.proc is None or self.inner.proc.poll() is not None:
            self.inner.start_sync()

    async def stop(self) -> None:
        """Parar el backend no para el motor: la grabación y el vídeo de los muros siguen."""

    def fresh_clients(self) -> None:
        """Clientes HTTP nuevos para un backend nuevo (los anteriores quedan ligados a su bucle de eventos)."""
        self.inner._api = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.inner.api_port}", timeout=5)
        self.inner._play = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.inner.playback_port}", timeout=10)

    def kill(self) -> int:
        proc = self.inner.proc
        assert proc is not None and proc.poll() is None
        os.kill(proc.pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
        proc.wait(timeout=10)
        return proc.pid

    def relaunch(self) -> int:
        """Lo que hará vmsctl: relanzar MediaMTX; las rutas vuelven solas de su YAML (S2: 0,2 s)."""
        self.inner.proc = None
        self.inner.start_sync()
        for name, conf in list(self.inner._applied.items()):
            r = httpx.post(f"http://127.0.0.1:{self.inner.api_port}/v3/config/paths/add/{quote(name, safe='/')}",
                           json=conf, timeout=5)
            r.raise_for_status()
        assert self.inner.proc is not None
        return self.inner.proc.pid


@dataclass
class Stack:
    sim: CameraSimulator
    engine: AttachedEngine
    options: StubOptions
    data: Path
    port: int
    backend: RealBackend
    server: ServerThread

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self) -> Any:
        return self.backend.app.state.vms

    def publish_engine(self, state: str, pid: int | None) -> None:
        """Lo que hará B1 al ver el motor reiniciado (CONTRATO §17.3), en el bucle del servidor."""
        loop = self.server.server.servers[0].get_loop()
        ev = {"state": state, "pid": pid, "at": datetime.now(timezone.utc).isoformat()}
        loop.call_soon_threadsafe(publish_engine, self.state.bus, ev)

    def restart_backend(self) -> float:
        t0 = time.monotonic()
        self.server.stop()
        self.engine.fresh_clients()
        self.backend = RealBackend(self.data, self.engine, self.options)  # type: ignore[arg-type]
        self.server = ServerThread(self.backend.app, self.port).start()
        return time.monotonic() - t0


@pytest.fixture
def stack(tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str) -> Iterator[Stack]:
    sim = CameraSimulator([SimDevice("hik1", "hikvision", channels=4)], tmp_path / "camsim",
                          mediamtx_bin=mediamtx_bin, ffmpeg_bin=ffmpeg_bin).start(wait_ready=60)
    inner = MtxTestEngine(mediamtx_bin, tmp_path / "engine")
    inner.start_sync()
    engine = AttachedEngine(inner)
    options = StubOptions(device_client=device_client_for(sim), discover=fake_discover, status_interval=2.0)
    data = tmp_path / "data"
    backend = RealBackend(data, engine, options)  # type: ignore[arg-type]
    port = get_free_port()
    server = ServerThread(backend.app, port).start()
    st = Stack(sim, engine, options, data, port, backend, server)
    try:
        dev = sim.device("hik1")
        with httpx.Client(base_url=st.base, headers={"X-Requested-With": "vms"}, timeout=30) as c:
            c.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]}).raise_for_status()
            r = c.post("/api/devices", json={"name": "NVR pruebas", "vendor": "hikvision", "kind": "nvr",
                                             "host": "127.0.0.1", "rtsp_port": dev.port, "username": dev.username,
                                             "password": dev.password, "import_channels": "all"})
            r.raise_for_status()
            ids = [cam["id"] for cam in sorted(c.get("/api/cameras").json(), key=lambda x: x["channel"])]
            assert len(ids) == 4
            c.put("/api/walls/1", json={"grid": 4, "cells": ids + [None] * 12}).raise_for_status()
            c.put("/api/walls/2", json={"grid": 1, "cells": [ids[0]] + [None] * 15}).raise_for_status()
        yield st
    finally:
        st.server.stop()
        inner.stop_sync()
        sim.stop()


def kiosk_enter(page: Any, token: str, wall: int) -> None:
    """Lo mismo que hace el visor (CONTRATO §17.1): página mínima del backend + fetch del intercambio."""
    page.goto("/api/local/kiosk")
    status = page.evaluate(
        """async ([token, next]) => (await fetch('/api/local/kiosk-session', {method: 'POST', credentials: 'same-origin',
             headers: {'Content-Type': 'application/json', 'X-Requested-With': 'vms'},
             body: JSON.stringify({token, next})})).status""", [token, f"/wall/{wall}"])
    assert status == 204
    page.goto(f"/wall/{wall}")
    page.wait_for_function("() => window.__vmsWall && window.__vmsWall.cells().length > 0", timeout=20000)


def playing(pages: list[Any], gap: float = 0.6) -> bool:
    """Todas las celdas con cámara en vivo, con imagen y con fotogramas nuevos."""
    before = [{c["i"]: c for c in p.evaluate(CELLS_JS)} for p in pages]
    time.sleep(gap)
    for p, prev in zip(pages, before):
        now = p.evaluate(CELLS_JS)
        if not now:
            return False
        for c in now:
            old = prev.get(c["i"])
            if c["state"] != "live" or c["w"] <= 0 or old is None or c["frames"] <= old["frames"]:
                return False
    return True


def wait_playing(pages: list[Any], timeout: float, t0: float | None = None) -> float:
    start = time.monotonic() if t0 is None else t0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if playing(pages):
            return time.monotonic() - start
    states = [[(c["i"], c["state"], c["frames"]) for c in p.evaluate(CELLS_JS)] for p in pages]
    raise AssertionError(f"Los muros no tienen vídeo a los {timeout:.0f} s: {states}")


@pytest.fixture
def walls(stack: Stack, make_context: Any) -> tuple[Any, list[Any], PageErrors]:
    ctx = make_context(stack.base)
    errors = PageErrors()
    pages = []
    for wall in (1, 2):
        page = ctx.new_page()
        errors.attach(page)
        kiosk_enter(page, stack.backend.opt.kiosk_token, wall)
        pages.append(page)
    wait_playing(pages, 60)
    time.sleep(5)   # muros funcionando unos segundos (como en una tienda): cada celda ya conoce sus fps
    return ctx, pages, errors


def test_walls_share_one_event_stream_and_enter_without_url_token(stack: Stack, walls: Any) -> None:
    _, pages, errors = walls
    for p in pages:
        assert "/wall/" in p.url and "token" not in p.url
        assert p.evaluate("() => window.__vmsWall.events.transport") == "shared-worker"
        me = p.evaluate("() => fetch('/api/auth/me').then(r => r.json())")
        assert me == {"username": "kiosco", "role": "kiosk", "kiosk": True}
    deadline = time.monotonic() + 10
    while stack.state.bus.subscribers != 1 and time.monotonic() < deadline:
        time.sleep(0.2)
    assert stack.state.bus.subscribers == 1, "dos muros, una sola conexión SSE"
    # un 404 del WHEP mientras el subflujo bajo demanda arranca es normal (el muro lo trata como «sin señal»)
    assert errors.unexpected(("404 (Not Found)",)) == []


def test_engine_killed_video_back_within_6s_with_engine_event(stack: Stack, walls: Any) -> None:
    _, pages, _ = walls
    results = []
    for _ in range(2):   # dos veces: la segunda con el muro ya «escarmentado» (contadores de espera usados)
        t0 = time.monotonic()
        stack.engine.kill()
        time.sleep(1.0)                      # vmsctl relanza el motor con espera 1 s (CONTRATO §14.1)
        pid = stack.engine.relaunch()
        stack.publish_engine("restarted", pid)
        took = wait_playing(pages, 30, t0)
        results.append(took)
        print(f"Motor matado → vídeo en todas las celdas de los 2 muros en {took:.2f} s")
        time.sleep(3)
    assert max(results) <= TARGET_S, f"vídeo de vuelta en {results} s (objetivo ≤ {TARGET_S} s)"
    restarts = [c["reader"]["restarts"] for p in pages for c in p.evaluate("() => window.__vmsWall.cells()")]
    assert all(r >= 2 for r in restarts), restarts


def test_engine_killed_without_event_recovers_on_its_own(stack: Stack, walls: Any) -> None:
    _, pages, _ = walls
    t0 = time.monotonic()
    stack.engine.kill()
    time.sleep(1.0)
    stack.engine.relaunch()
    took = wait_playing(pages, 40, t0)
    print(f"Sin evento «engine»: vídeo de vuelta en {took:.2f} s")
    assert took <= FALLBACK_S, f"{took:.1f} s"


def test_backend_restart_keeps_walls_without_login_and_video(stack: Stack, walls: Any) -> None:
    _, pages, errors = walls
    reconnects_before = [c["reader"]["reconnects"] for p in pages for c in p.evaluate("() => window.__vmsWall.cells()")]
    frames_before = [sum(c["frames"] for c in p.evaluate(CELLS_JS)) for p in pages]
    took = stack.restart_backend()
    # durante el reinicio el vídeo no se corta: va directo del motor al muro (WebRTC)
    frames_during = [sum(c["frames"] for c in p.evaluate(CELLS_JS)) for p in pages]
    assert all(b > a for a, b in zip(frames_before, frames_during)), (frames_before, frames_during)
    time.sleep(6)   # la conexión SSE vuelve (retry 3 s) y el muro relee su configuración
    for p in pages:
        assert "/wall/" in p.url and "/login" not in p.url
        status = p.evaluate("() => fetch('/api/auth/me').then(r => r.status)")
        assert status == 200, "la cookie de kiosco sigue valiendo tras reiniciar el backend"
    assert playing(pages)
    reconnects_after = [c["reader"]["reconnects"] for p in pages for c in p.evaluate("() => window.__vmsWall.cells()")]
    assert reconnects_after == reconnects_before, "ninguna celda tuvo que reconectar"
    deadline = time.monotonic() + 15
    while stack.state.bus.subscribers != 1 and time.monotonic() < deadline:
        time.sleep(0.2)
    assert stack.state.bus.subscribers == 1, "la conexión de eventos compartida volvió"
    print(f"Backend reiniciado en {took:.1f} s sin cortar el vídeo ni pedir login")
    # los errores de red mientras el backend estaba parado son esperables (SSE, /api/walls)
    assert [e for e in errors.unexpected(("ERR_CONNECTION_REFUSED", "Failed to fetch", "net::"))
            if "pageerror" in e] == []
