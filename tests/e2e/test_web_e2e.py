"""Extremo a extremo de la interfaz con vídeo real, sin hardware:

    simulador de cámaras (rutas nativas Hikvision/Dahua + Digest)
      → MediaMTX real (rutas añadidas por API, WebRTC, grabación fMP4, playback)
      → backend de pruebas (API del contrato + proxy WHEP y de reproducción)
      → Chromium (Playwright) con la interfaz real.

Comprueba en el navegador: alta de equipos desde el formulario, asignación a un monitor, vídeo
WebRTC en el muro (currentTime avanza y videoWidth > 0), ampliar celda con el flujo principal,
recuperación tras cortar el flujo de la cámara sin conexiones fantasma, reproducción de un tramo
grabado, descarga de clip, snapshot para dibujar zonas y capturas de cada página.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.web.conftest import (ADMIN, PageErrors, backend_class, login, make_context, pw_browser,  # noqa: F401
                                screenshots_dir)
from tests.web.demo import ServerThread, start_demo
from tests.web.stub_backend import StubBackend

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg,
              pytest.mark.needs_browser, pytest.mark.timeout(420)]


class Env:
    def __init__(self, sim: Any, engine: Any, backend: StubBackend, server: ServerThread) -> None:
        self.sim, self.engine, self.backend, self.server = sim, engine, backend, server
        self.api = httpx.Client(base_url=server.base_url, headers={"X-Requested-With": "vms"}, timeout=15)
        self.api.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]}).raise_for_status()


@pytest.fixture
def env(tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str, people_video: Path) -> Iterator[Env]:
    sim, engine, options = start_demo(tmp_path / "demo", mediamtx_bin=mediamtx_bin, ffmpeg_bin=ffmpeg_bin)
    backend = server = None
    try:
        backend = backend_class()(tmp_path / "data", engine, options)
        server = ServerThread(backend.app).start()
        e = Env(sim, engine, backend, server)
        yield e
        e.api.close()
    finally:
        if server is not None:
            server.stop()
        engine.stop_sync()
        sim.stop()


def add_device_via_form(page: Any, name: str, vendor: str, port: int, password: str) -> None:
    page.click("#btn-add-device")
    page.fill("#dev-name", name)
    page.select_option("#dev-vendor", vendor)
    page.select_option("#dev-kind", "nvr")
    page.fill("#dev-host", "127.0.0.1")
    page.fill("#dev-rtsp-port", str(port))
    page.fill("#dev-username", "admin")
    page.fill("#dev-password", password)
    page.click("#btn-device-test")
    page.wait_for_selector("#test-channels .channel-item")
    page.click("#btn-device-save")
    page.wait_for_selector("#device-dialog", state="hidden")


def wait_video_playing(page: Any, selector: str, timeout: float = 40.0) -> dict[str, float]:
    """Espera a que el <video> tenga imagen y su currentTime avance de verdad."""
    deadline = time.monotonic() + timeout
    first: dict[str, float] | None = None
    last: dict[str, float] = {}
    while time.monotonic() < deadline:
        last = page.eval_on_selector(selector, "v => ({t: v.currentTime, w: v.videoWidth, h: v.videoHeight})")
        if last["w"] > 0:
            if first is None:
                first = last
            elif last["t"] > first["t"] + 1.0:
                return last
        time.sleep(0.5)
    raise AssertionError(f"El vídeo {selector} no avanza: primero={first} último={last}")


def cell_state(page: Any, index: int) -> str:
    return page.get_attribute(f".cell[data-index='{index}']", "data-state") or ""


def test_full_flow_live_wall_playback_and_pages(env: Env, make_context: Any, screenshots_dir: Path) -> None:  # noqa: F811
    hik, dah = env.sim.device("hik1"), env.sim.device("dah1")
    ctx = make_context(env.server.base_url, viewport={"width": 1600, "height": 900})
    page = ctx.new_page()
    errors = PageErrors().attach(page)

    # ---------------------------------------------------------------- alta de equipos desde el formulario
    login(page)
    add_device_via_form(page, "NVR Hikvision entrada", "hikvision", hik.port, hik.password)
    add_device_via_form(page, "NVR Dahua cajas", "dahua", dah.port, dah.password)
    page.wait_for_function("document.querySelectorAll('#cameras-table tr[data-camera]').length === 4")
    cams = env.api.get("/api/cameras").json()
    by_dev: dict[str, list[dict[str, Any]]] = {}
    for c in cams:
        by_dev.setdefault(c["device_name"], []).append(c)
    hik_cams = sorted(by_dev["NVR Hikvision entrada"], key=lambda c: c["channel"])
    dah_cams = sorted(by_dev["NVR Dahua cajas"], key=lambda c: c["channel"])

    # ---------------------------------------------------------------- asignación al monitor 1
    page.wait_for_timeout(800)   # deja que terminen los refrescos por eventos del alta
    page.evaluate("document.getElementById('sec-monitors').scrollIntoView()")  # paleta y celdas a la vista
    page.click("#tab-monitor-1")
    page.click("#grid-buttons [data-grid='4']")
    page.fill("#wall-name", "Entrada y cajas")
    page.select_option("[data-slot-select='0']", hik_cams[0]["id"])   # canal 1 = vídeo de personas
    page.drag_and_drop(f".cam-chip[data-camera='{dah_cams[0]['id']}']", "[data-slot='1']")
    page.select_option("[data-slot-select='2']", hik_cams[1]["id"])
    page.click("#btn-save-wall")
    page.wait_for_selector("#wall-dirty", state="hidden")
    assert env.api.get("/api/walls/1").json()["cells"][:4] == [hik_cams[0]["id"], dah_cams[0]["id"],
                                                                hik_cams[1]["id"], None]
    # la grabación 24/7 del principal arranca sola: esperamos a que el estado lo refleje
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        st = env.api.get("/api/status").json()
        if sum(1 for c in st["cameras"] if c["online"] and c["recording"]) == 4:
            break
        time.sleep(1)
    else:
        raise AssertionError(f"Las cámaras no quedaron en línea y grabando: {st['cameras']}")
    page.goto("/")
    page.wait_for_selector("#cameras-table .pill.ok")
    page.screenshot(path=str(screenshots_dir / "e2e-panel.png"))

    # ---------------------------------------------------------------- muro con WebRTC real
    wall = ctx.new_page()
    wall_errors = PageErrors().attach(wall)
    wall.goto("/wall/1")
    for i in range(3):
        info = wait_video_playing(wall, f".cell[data-index='{i}'] video")
        assert info["w"] == 320, f"la celda {i} debe usar el subflujo (320x180), no {info}"
    wall.wait_for_function("[0, 1, 2].every(i => document.querySelector(`.cell[data-index='${i}']`).dataset.state === 'live')")
    assert wall.text_content(".cell[data-index='0'] .badge") == "En vivo"
    assert wall.locator(".cell").count() == 4
    wall.mouse.move(640, 300)
    wall.screenshot(path=str(screenshots_dir / "muro-monitor-1.png"))
    assert wall.evaluate("() => window.__pcTracker.open") == 3

    # doble clic: pantalla completa de una celda con el flujo principal (640x360)
    wall.dblclick(".cell[data-index='0']")
    wall.wait_for_selector(".wall.expanded .cell.is-expanded")
    info = wait_video_playing(wall, ".cell[data-index='0'] video")
    assert info["w"] == 640, f"la celda ampliada debe usar el flujo principal: {info}"
    assert wall.evaluate("() => window.__pcTracker.open") == 1, "las demás celdas paran su vídeo al ampliar"
    wall.screenshot(path=str(screenshots_dir / "muro-celda-ampliada.png"))
    wall.dblclick(".cell[data-index='0']")
    wall.wait_for_selector(".wall:not(.expanded)")
    for i in range(3):
        wait_video_playing(wall, f".cell[data-index='{i}'] video")

    # ---------------------------------------------------------------- corte del flujo y recuperación
    env.sim.kill_stream("dah1", 1, "sub")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and cell_state(wall, 1) == "live":
        time.sleep(0.5)
    assert cell_state(wall, 1) in ("reconnecting", "offline"), "el muro debe detectar el corte"
    assert cell_state(wall, 0) == "live", "las demás celdas no se ven afectadas"
    env.sim.start_stream("dah1", 1, "sub")
    wall.wait_for_function("document.querySelector(\".cell[data-index='1']\").dataset.state === 'live'", timeout=60000)
    wait_video_playing(wall, ".cell[data-index='1'] video")
    assert wall.evaluate("() => window.__pcTracker.open") == 3, "no deben quedar conexiones WebRTC fantasma"
    reconnects = wall.evaluate("() => window.__vmsWall.cells().find(c => c.index === 1).reader")
    assert reconnects["attempts"] >= 2

    # cambio de layout desde el panel: el muro se adapta sin recargar
    wall.evaluate("() => { window.__noReload = true; }")
    env.api.put("/api/walls/1", json={"grid": 1}).raise_for_status()
    wall.wait_for_function("document.querySelectorAll('.cell').length === 1", timeout=15000)
    info = wait_video_playing(wall, ".cell[data-index='0'] video")
    assert info["w"] == 640, "con distribución de 1 se usa el flujo principal"
    assert wall.evaluate("() => window.__noReload") is True
    assert wall.evaluate("() => window.__pcTracker.open") == 1
    assert wall_errors.unexpected(("401 (Unauthorized)",)) == []
    wall.close()

    # ---------------------------------------------------------------- reproducción de lo grabado
    cam_id = hik_cams[0]["id"]
    deadline = time.monotonic() + 40
    spans: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        spans = env.api.get(f"/api/recordings/{cam_id}/timeline").json()["spans"]
        if spans and spans[-1]["duration"] >= 6:
            break
        time.sleep(1)
    assert spans and spans[-1]["duration"] >= 6, f"sin grabación suficiente: {spans}"

    page.goto(f"/playback?camera={cam_id}")
    page.wait_for_function("() => window.__vmsPlayback.spans.length > 0")
    page.click("#pb-zoom [data-hours='1']")
    span = page.evaluate("() => window.__vmsPlayback.spans.at(-1)")
    view = page.evaluate("() => window.__vmsPlayback.view")
    from datetime import datetime
    ts = {k: datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() for k, v in
          {"v0": view["from"], "v1": view["to"], "s0": span["start"], "s1": span["end"]}.items()}
    target = ts["s0"] + 1.5
    box = page.locator("#timeline-track").bounding_box()
    page.mouse.click(box["x"] + (target - ts["v0"]) / (ts["v1"] - ts["v0"]) * box["width"],
                     box["y"] + box["height"] / 2)
    info = wait_video_playing(page, "#pb-video", timeout=30)
    assert info["w"] == 640, f"la reproducción usa el flujo principal grabado (640x360): {info}"
    assert page.is_hidden("#pb-overlay")
    pos_before = page.evaluate("() => window.__vmsPlayback.position")
    page.click("[data-seek='-10']")
    time.sleep(1.0)
    pos_after = page.evaluate("() => window.__vmsPlayback.position")
    assert pos_before and pos_after and pos_after < pos_before, (pos_before, pos_after)
    page.screenshot(path=str(screenshots_dir / "reproduccion.png"))

    # descarga de un clip MP4 (por la API del navegador, con la sesión)
    page.fill("#clip-start", time.strftime("%H:%M:%S", time.localtime(ts["s0"] + 1)))
    page.select_option("#clip-duration", "30")
    href = page.get_attribute("#clip-download", "href")
    r = page.request.get(href)
    assert r.status == 200, r.text()[:300]
    assert r.headers["content-type"] == "video/mp4"
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.body()
    assert len(body) > 10_000 and body[4:8] == b"ftyp"

    # ---------------------------------------------------------------- analítica con imagen real (en memoria)
    page.goto(f"/analytics?camera={cam_id}")
    page.wait_for_function("() => document.querySelector('#an-image') && document.querySelector('#an-image').naturalWidth > 0",
                           timeout=30000)
    assert page.evaluate("() => document.querySelector('#an-image').src.startsWith('blob:')")
    page.click("#tool-line")
    box = page.locator("#an-canvas").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.15, box["y"] + box["height"] * 0.7)
    page.mouse.click(box["x"] + box["width"] * 0.85, box["y"] + box["height"] * 0.7)
    page.fill("#rule-name", "Puerta")
    page.click("#rule-save")
    page.wait_for_selector("#an-rules [data-rule]")
    page.screenshot(path=str(screenshots_dir / "analitica-camara-real.png"))
    # RGPD: el snapshot no se ha escrito en ninguna carpeta de datos
    jpgs = [p for p in Path(env.backend.paths.base).rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    assert jpgs == []

    # ---------------------------------------------------------------- estado
    page.goto("/status")
    page.wait_for_selector("#st-cams-table .pill.ok")
    assert page.inner_text("#st-cams").startswith("4")
    page.screenshot(path=str(screenshots_dir / "estado-real.png"))
    assert errors.unexpected(("401 (Unauthorized)",)) == []


def test_wall_layout_churn_does_not_leak_connections(env: Env, make_context: Any) -> None:  # noqa: F811
    """Muchos cambios de layout seguidos (como un operador durante días): al final solo quedan
    abiertas las conexiones WebRTC de las celdas visibles y el vídeo sigue en marcha."""
    hik, dah = env.sim.device("hik1"), env.sim.device("dah1")
    for name, vendor, dev in (("H", "hikvision", hik), ("D", "dahua", dah)):
        env.api.post("/api/devices", json={"name": name, "vendor": vendor, "kind": "nvr", "host": "127.0.0.1",
                                           "rtsp_port": dev.port, "username": "admin", "password": dev.password,
                                           "import_channels": "all"}).raise_for_status()
    ids = [c["id"] for c in env.api.get("/api/cameras").json()]
    assert len(ids) == 4
    env.api.put("/api/walls/2", json={"grid": 4, "cells": ids}).raise_for_status()
    deadline = time.monotonic() + 30   # el motor ya tiene las rutas (antes, MediaMTX aún no las conoce)
    while time.monotonic() < deadline:
        if sum(1 for c in env.api.get("/api/status").json()["cameras"] if c["online"]) == 4:
            break
        time.sleep(0.5)

    page = make_context(env.server.base_url, viewport={"width": 1280, "height": 720}).new_page()
    errors = PageErrors().attach(page)
    login(page, next_path="/wall/2")
    for i in range(4):
        wait_video_playing(page, f".cell[data-index='{i}'] video")
    heap0 = page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : 0")

    sequence = [1, 9, 4, 16, 1, 4, 9, 4]
    for grid in sequence:
        env.api.put("/api/walls/2", json={"grid": grid}).raise_for_status()
        page.wait_for_function(f"document.querySelectorAll('.cell').length === {grid}", timeout=15000)
        time.sleep(1.5)
    # con 4 celdas y 4 cámaras: todas en vivo y exactamente 4 conexiones abiertas
    for i in range(4):
        wait_video_playing(page, f".cell[data-index='{i}'] video", timeout=60)
    tracker = page.evaluate("() => ({open: window.__pcTracker.open, created: window.__pcTracker.created})")
    assert tracker["open"] == 4, tracker
    assert tracker["created"] >= 4 + 1, "los cambios de layout deben haber creado y cerrado conexiones"
    # MediaMTX también ve solo 4 lectores WebRTC (las sesiones viejas se cerraron con DELETE o por caída)
    deadline = time.monotonic() + 20
    readers = -1
    while time.monotonic() < deadline:
        sessions = httpx.get(f"http://127.0.0.1:{env.engine.api_port}/v3/webrtcsessions/list").json()
        readers = sum(1 for s in sessions.get("items", []) if s.get("state") in ("read", "reading"))
        if readers == 4:
            break
        time.sleep(1)
    assert readers == 4, f"sesiones WebRTC en el servidor: {readers}"
    page.evaluate("() => window.gc && window.gc()")
    heap1 = page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : 0")
    if heap0 and heap1:
        assert heap1 < heap0 * 3 + 5_000_000, f"crecimiento de memoria JS sospechoso: {heap0} → {heap1}"
    assert errors.unexpected(("401 (Unauthorized)",)) == []
