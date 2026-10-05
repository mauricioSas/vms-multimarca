"""Muro y panel ante una versión nueva y reglas de reintento del lector WHEP (B2).

- Evento SSE `update` con otra versión → el muro se recarga UNA vez, solo (CONTRATO §17.3).
- `banner.js` (panel) → aviso «Hay una versión nueva…» cuando /api/health cambia de versión.
- Reglas puras de `whep.js`: tope de 2 s con el motor caído o recuperándose, y tiempo de «vídeo parado» según fps.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import get_free_port
from tests.fakes import FakeEngine
from tests.web.conftest import ADMIN, PageErrors, login
from tests.web.demo import ServerThread
from tests.web.real_backend import RealBackend
from tests.web.stub_backend import StubOptions, fake_discover
from vms.api.events import publish_update

pytestmark = [pytest.mark.needs_browser]


@pytest.fixture
def real(tmp_path: Path) -> Iterator[tuple[RealBackend, ServerThread]]:
    backend = RealBackend(tmp_path / "data", FakeEngine(), StubOptions(discover=fake_discover))
    server = ServerThread(backend.app, get_free_port()).start()
    try:
        yield backend, server
    finally:
        server.stop()


def in_loop(server: ServerThread, fn: Any, *args: Any) -> None:
    server.server.servers[0].get_loop().call_soon_threadsafe(fn, *args)


def test_whep_retry_and_stall_rules(real: Any, make_context: Any) -> None:
    _, server = real
    page = make_context(server.base_url).new_page()
    page.goto("/login")
    r = page.evaluate("""async () => {
      const m = await import('/static/js/whep.js');
      const b = [1000, 2000, 5000, 10000, 30000];
      return {
        normal: [0, 1, 2, 3, 9].map(a => m.retryDelay(b, a)),
        engineDown: [0, 3].map(a => m.retryDelay(b, a, {status: 503})),
        badGateway: m.retryDelay(b, 4, {status: 502}),
        notFound: m.retryDelay(b, 3, {status: 404}),
        recovering404: m.retryDelay(b, 3, {status: 404, recovering: true}),
        stall: [0, 0.5, 1, 2.5, 5, 10, 30].map(f => m.stallLimit(f)),
        recBefore: m.engineRecovering(1e12),
        recAfter: (m.noteEngineEvent(1e12), m.engineRecovering(1e12 + 59000)),
        recExpired: m.engineRecovering(1e12 + 61000),
      };
    }""")
    assert r["normal"] == [1000, 2000, 5000, 10000, 30000]
    assert r["engineDown"] == [1000, 2000], "motor caído (503): nunca más de 2 s entre intentos"
    assert r["badGateway"] == 2000
    assert r["notFound"] == 10000, "cámara sin señal (404) fuera de una caída del motor: espera normal"
    assert r["recovering404"] == 2000
    assert r["stall"] == [12000, 12000, 12000, 12000, 6000, 3000, 3000]
    assert (r["recBefore"], r["recAfter"], r["recExpired"]) == (False, True, False)


def kiosk_wall(page: Any, token: str, wall: int = 1) -> None:
    page.goto("/api/local/kiosk")
    st = page.evaluate("""async ([token, next]) => (await fetch('/api/local/kiosk-session', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Requested-With': 'vms'},
        body: JSON.stringify({token, next})})).status""", [token, f"/wall/{wall}"])
    assert st == 204
    page.goto(f"/wall/{wall}")
    page.wait_for_function("() => window.__vmsWall && window.__vmsWall.events.serverVersion && "
                           "window.__vmsWall.events.opens > 0", timeout=20000)


def test_wall_reloads_once_when_the_service_version_changes(real: Any, make_context: Any) -> None:
    backend, server = real
    page = make_context(server.base_url).new_page()
    errors = PageErrors().attach(page)
    kiosk_wall(page, backend.opt.kiosk_token)
    current = page.evaluate("() => window.__vmsWall.events.serverVersion")
    page.evaluate("() => { window.__marca = 1; }")
    # misma versión (p. ej. el aviso de un estado intermedio): no recarga
    in_loop(server, publish_update, backend.app.state.vms.bus, {"version": current, "state": "verifying"})
    time.sleep(3)
    assert page.evaluate("() => window.__marca") == 1
    # versión nueva: recarga una vez, entre 2 y 8 s después, y sigue en el muro sin pedir login
    t0 = time.monotonic()
    in_loop(server, publish_update, backend.app.state.vms.bus,
            {"version": "9.9.9", "viewer_restart": False, "state": "good", "message_es": ""})
    page.wait_for_function("() => window.__marca === undefined", timeout=15000)
    took = time.monotonic() - t0
    assert 1.5 <= took <= 12, took
    page.wait_for_function("() => window.__vmsWall && window.__vmsWall.events.opens > 0", timeout=20000)
    assert page.url.endswith("/wall/1")
    assert errors.unexpected() == []


def test_panel_banner_announces_new_version(real: Any, make_context: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, server = real
    page = make_context(server.base_url).new_page()
    login(page, ADMIN, "/status")
    page.add_script_tag(url="/static/js/banner.js", type="module")
    page.wait_for_function("() => window.__vmsUpdateBanner && window.__vmsUpdateBanner.initial", timeout=10000)
    assert page.locator("#vms-update-banner").count() == 0
    import vms.api.routes.system as system_routes
    monkeypatch.setattr(system_routes, "__version__", "9.9.9")
    page.evaluate("() => window.__vmsUpdateBanner.check()")
    banner = page.locator("#vms-update-banner")
    banner.wait_for(timeout=5000)
    assert "versión nueva del servicio (9.9.9)" in banner.inner_text()
    assert banner.get_attribute("role") == "status"
