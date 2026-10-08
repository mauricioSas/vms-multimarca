"""Reproducción en Chromium contra el backend REAL: una línea de tiempo vacía dice por qué.

- Cámara que nunca grabó → «Esta cámara aún no tiene grabaciones» con «Qué hacer:», sin avisos de error.
- Motor caído → mensaje fijo «No se pueden consultar las grabaciones» (nunca «Sin grabaciones este día») y un solo
  aviso flotante aunque la línea de tiempo se refresque varias veces (antes salía uno nuevo cada 30 s).
- Estado del sistema: una cámara con vídeo que no graba sale «No graba» en rojo con el motivo.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fakes import FakeEngine
from tests.web.demo import ServerThread
from tests.web.real_backend import RealBackend
from tests.web.stub_backend import DEFAULT_USERS
from vms.core.errors import EngineUnavailable
from vms.core.recordings import PLAYBACK_ENGINE_DOWN
from vms.engine.engine import explain_recorder_error

pytestmark = pytest.mark.needs_browser
ADMIN = ("admin", DEFAULT_USERS["admin"][0])
CONTEXTS: list[Any] = []


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[RealBackend, ServerThread]]:
    backend = RealBackend(tmp_path / "datos", FakeEngine())
    srv = ServerThread(backend.app).start()
    try:
        yield backend, srv
    finally:
        while CONTEXTS:
            CONTEXTS.pop().close()
        srv.stop()


def _camera(srv: ServerThread) -> str:
    with httpx.Client(base_url=srv.base_url, headers={"X-Requested-With": "vms"}, timeout=10) as c:
        c.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]}).raise_for_status()
        c.put("/api/onboarding/state", json={"wizard_completed": True})
        dev = c.post("/api/devices", json={"name": "Cámara puerta", "vendor": "hikvision", "kind": "camera",
                                           "host": "192.0.2.10", "username": "vms", "password": "Lectura#2026",
                                           "import_channels": [1]}).json()
        return str(dev["cameras"][0])


def _open_playback(browser: Any, srv: ServerThread, cam: str, next_url: str = "") -> Any:
    ctx = browser.new_context(base_url=srv.base_url, viewport={"width": 1400, "height": 900}, locale="es-ES",
                              timezone_id="Europe/Madrid")
    CONTEXTS.append(ctx)
    page = ctx.new_page()
    page.goto(f"/login?next={next_url or f'/playback?camera={cam}'}")
    page.fill("#username", ADMIN[0])
    page.fill("#password", ADMIN[1])
    page.click("#login-submit")
    page.wait_for_url(lambda url: "/login" not in url)
    return page


def _timeline_reply(status: int) -> Any:
    return lambda r: "/api/recordings/" in r.url and "/timeline" in r.url and r.status == status


def test_camera_that_never_recorded_explains_why(browser: Any, server: tuple[RealBackend, ServerThread]) -> None:
    backend, srv = server
    cam = _camera(srv)
    engine = backend.engine
    assert isinstance(engine, FakeEngine)
    engine.spans[cam] = []
    backend.paths.recordings_dir.mkdir(parents=True, exist_ok=True)   # existe la carpeta general, no la de la cámara
    page = _open_playback(browser, srv, cam)
    page.wait_for_selector("#pb-overlay >> text=Esta cámara aún no tiene grabaciones")
    assert "Qué hacer:" in page.inner_text("#pb-overlay")
    assert page.inner_text("#timeline-summary") == "Ninguna grabación guardada"
    assert page.evaluate("() => window.__vmsPlayback.notice.code") == "never_recorded"
    assert page.locator(".toast.bad").count() == 0


def test_status_page_shows_a_camera_with_video_that_does_not_record(
        browser: Any, server: tuple[RealBackend, ServerThread]) -> None:
    """Estado del sistema: «No graba» en rojo con el motivo, y el aviso de arriba en rojo con su «Qué hacer:» (antes,
    «Grabando» con solo tener vídeo)."""
    backend, srv = server
    cam = _camera(srv)
    engine = backend.engine
    assert isinstance(engine, FakeEngine)
    reason = explain_recorder_error("write D:\\VMS\\x.mp4: There is not enough space on the disk.")
    engine.not_saving[cam] = reason
    page = _open_playback(browser, srv, cam, "/status")
    row = page.locator("#st-cams-table tbody tr", has_text=cam)
    row.locator(".pill.bad", has_text="No graba").wait_for()
    assert "el disco de grabación está lleno" in row.inner_text()
    banner = page.locator("#st-overall")
    assert "bad" in (banner.get_attribute("class") or "")
    assert "tiene vídeo pero no graba" in banner.inner_text() and "Qué hacer:" in banner.inner_text()
    assert page.locator("#st-cams-table .pill.ok", has_text="Grabando").count() == 0


def test_engine_down_is_not_shown_as_no_recordings(browser: Any, server: tuple[RealBackend, ServerThread]) -> None:
    backend, srv = server
    cam = _camera(srv)
    engine = backend.engine
    assert isinstance(engine, FakeEngine)
    engine.list_errors[cam] = EngineUnavailable(PLAYBACK_ENGINE_DOWN)
    page = _open_playback(browser, srv, cam)
    page.wait_for_selector("#pb-overlay >> text=No se pueden consultar las grabaciones")
    assert "Qué hacer:" in page.inner_text("#pb-overlay")
    for _ in range(2):   # dos refrescos más (como los de cada 30 s): el mismo problema no repite el aviso
        with page.expect_response(_timeline_reply(503)):
            page.click("#pb-reload")
    page.wait_for_timeout(300)
    assert page.inner_text("#timeline-summary") == "No se pueden consultar las grabaciones"
    assert page.locator(".toast.bad", has_text="No se pudo leer la línea de tiempo").count() == 1
    # Vuelve el motor: desaparece el problema y se ven los tramos
    del engine.list_errors[cam]
    with page.expect_response(_timeline_reply(200)):
        page.click("#pb-reload")
    page.wait_for_selector("#timeline-spans i")
    assert page.evaluate("() => window.__vmsPlayback.problem") == ""
    assert "Elige un momento" in page.inner_text("#pb-overlay")
