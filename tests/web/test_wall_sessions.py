"""Sesión del panel y de los muros, y conexiones de eventos por página (revisión de la interfaz v2).

- **Crítico 1:** panel y muros en el MISMO perfil del navegador (un contexto de Playwright = un perfil de
  WebView2, como en el visor antes de separar sus carpetas de datos): la cookie de kiosco de los muros no puede
  echar al operador del panel, ni el inicio o cierre de sesión del panel convertir los muros en operador.
- **Alto 4:** una sola conexión `/api/events` por página (la segunda que abría `ops-common.js` agotaba el cupo
  de 6 conexiones de Chromium con varias pestañas y colgaba el muro); los 4 muros siguen compartiendo UNA.

Backend real (`vms.api`) con FakeEngine (sin vídeo) + Chromium.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fakes import FakeEngine
from tests.web.conftest import ADMIN, OPERATOR, PageErrors, login
from tests.web.demo import ServerThread
from tests.web.real_backend import RealBackend
from tests.web.stub_backend import StubOptions, fake_discover

pytestmark = [pytest.mark.needs_browser, pytest.mark.timeout(240)]

EXCHANGE_JS = """async ([token, next]) => {
  const r = await fetch('/api/local/kiosk-session', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Requested-With': 'vms'}, body: JSON.stringify({token, next})});
  return r.status;
}"""
ME_JS = "async () => (await fetch('/api/auth/me', {headers: {'X-Requested-With': 'vms'}})).json()"


class Site:
    def __init__(self, backend: RealBackend, server: ServerThread, c1: str, c2: str) -> None:
        self.backend, self.server, self.c1, self.c2 = backend, server, c1, c2

    @property
    def base(self) -> str:
        return self.server.base_url

    @property
    def subscribers(self) -> int:
        return int(self.backend.app.state.vms.bus.subscribers)

    def wait_subscribers(self, n: int, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.subscribers == n:
                return n
            time.sleep(0.1)
        return self.subscribers


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    """NVR con 2 cámaras en el muro 1; el operador solo tiene la primera (vivo y grabaciones)."""
    backend = RealBackend(tmp_path / "data", FakeEngine(), StubOptions(discover=fake_discover))
    server = ServerThread(backend.app).start()
    try:
        with httpx.Client(base_url=server.base_url, headers={"X-Requested-With": "vms"}, timeout=10) as a:
            a.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]}).raise_for_status()
            r = a.post("/api/devices", json={"name": "NVR Tienda", "vendor": "hikvision", "kind": "nvr",
                                             "host": "10.0.0.5", "username": "admin", "password": "Cl@ve#1",
                                             "import_channels": [1, 2]})
            assert r.status_code == 201, r.text
            c1, c2 = r.json()["cameras"]
            r = a.patch("/api/users/operador", json={"camera_scope": {"cameras": [c1], "live": True,
                                                                      "playback": True, "export": False}})
            assert r.status_code == 200, r.text
            a.put("/api/walls/1", json={"grid": 4, "cells": [c1, c2]}).raise_for_status()
        yield Site(backend, server, c1, c2)
    finally:
        server.stop()


def enter_wall(ctx: Any, site: Site, wall: int) -> Any:
    """Lo que hace el visor (kiosk.rs): página de intercambio, `fetch` con el token y luego `/wall/N`."""
    page = ctx.new_page()
    page.goto("/api/local/kiosk")
    assert page.evaluate(EXCHANGE_JS, [site.backend.opt.kiosk_token, f"/wall/{wall}"]) == 204
    page.goto(f"/wall/{wall}")
    page.wait_for_function("() => window.__vmsWall && window.__vmsWall.cells().length === 4", timeout=15000)
    return page


def wall_view(page: Any) -> dict[str, Any]:
    return page.evaluate("""() => ({kiosk: window.__vmsWall.kiosk, panelLink: document.getElementById('link-panel').hidden,
      cells: window.__vmsWall.cells().slice(0, 2).map(c => c.cameraId),
      notices: [...document.querySelectorAll('.cell')].slice(0, 2).map(c => c.querySelector('.notice strong').textContent)})""")


def test_panel_and_walls_in_the_same_profile_keep_their_own_session(site: Site,
                                                                     make_context: Callable[..., Any]) -> None:
    ctx = make_context(site.base)                 # UN perfil para el panel y los muros
    panel = ctx.new_page()
    errors = PageErrors().attach(panel)
    login(panel, OPERATOR, "/")
    assert panel.evaluate(ME_JS)["username"] == "operador"

    wall = enter_wall(ctx, site, 1)
    errors.attach(wall)
    # 1) abrir los muros no echa al operador del panel
    assert panel.evaluate(ME_JS) == {"username": "operador", "role": "operator", "kiosk": False}
    panel.goto("/status")
    assert panel.url.endswith("/status"), panel.url
    # el muro es kiosco: ve las dos cámaras del muro, sin enlace al panel
    view = wall_view(wall)
    assert view["kiosk"] is True and view["panelLink"] is True, view
    assert view["cells"] == [site.c1, site.c2] and "Cámara no disponible" not in view["notices"], view

    # 2) el operador vuelve a iniciar sesión en el panel: el muro sigue siendo kiosco (no pasa a su ámbito)
    status = panel.evaluate("""async ([u, p]) => (await fetch('/api/auth/login', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'vms'},
      body: JSON.stringify({username: u, password: p})})).status""", list(OPERATOR))
    assert status == 200
    wall.reload()
    wall.wait_for_function("() => window.__vmsWall && window.__vmsWall.cells().length === 4 && window.__vmsWall.kiosk !== null",
                           timeout=15000)
    view = wall_view(wall)
    assert view["kiosk"] is True and "Cámara no disponible" not in view["notices"], view

    # 3) cerrar sesión en el panel no toca el muro, y el panel vuelve al login (no salta al muro)
    panel.evaluate("async () => fetch('/api/auth/logout', {method: 'POST', headers: {'X-Requested-With': 'vms'}})")
    wall.reload()
    wall.wait_for_function("() => window.__vmsWall && window.__vmsWall.kiosk === true", timeout=15000)
    assert wall.url.endswith("/wall/1"), wall.url
    panel.goto("/")
    assert "/login" in panel.url, panel.url
    time.sleep(1.0)                                # login.js no debe saltar al muro con la sesión de kiosco
    assert "/login" in panel.url and panel.is_visible("#login-form"), panel.url
    assert not errors.unexpected(("401", "503", "/whep")), errors.errors   # 503: FakeEngine sin vídeo


def test_viewer_profiles_walls_share_one_event_stream(site: Site, make_context: Callable[..., Any]) -> None:
    """Como en el visor: el panel en su perfil y los 4 muros en otro (compartido entre ellos)."""
    panel_ctx = make_context(site.base)
    walls_ctx = make_context(site.base)
    panel = panel_ctx.new_page()
    login(panel, OPERATOR, "/")
    assert site.wait_subscribers(1) == 1
    walls = [enter_wall(walls_ctx, site, n) for n in (1, 2, 3, 4)]
    # 4 muros = 1 conexión (SharedWorker) + la del panel
    assert site.wait_subscribers(2) == 2, site.subscribers
    assert {w.evaluate("window.__vmsWall.events.transport") for w in walls} == {"shared-worker"}
    assert walls[0].evaluate("window.__vmsWall.kiosk") is True
    assert panel.evaluate(ME_JS)["username"] == "operador"


@pytest.mark.parametrize("path", ["/", "/status", "/playback", "/analytics"])
def test_each_panel_page_opens_one_event_stream(site: Site, make_context: Callable[..., Any], path: str) -> None:
    ctx = make_context(site.base)
    page = ctx.new_page()
    login(page, ADMIN, path)
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)
    assert site.subscribers <= 1, f"{path}: {site.subscribers} conexiones /api/events"


def test_many_tabs_do_not_starve_the_wall(site: Site, make_context: Callable[..., Any]) -> None:
    """Panel + estado + muro + grabaciones en el mismo navegador (cupo de 6 conexiones HTTP/1.1 por servidor):
    la pestaña nueva carga y el muro sigue pudiendo hablar con el servidor."""
    ctx = make_context(site.base)
    first = ctx.new_page()
    login(first, ADMIN, "/")
    status = ctx.new_page()
    status.goto("/status")
    status.wait_for_load_state("load")
    wall = ctx.new_page()
    wall.goto("/wall/1")
    wall.wait_for_function("() => window.__vmsWall && window.__vmsWall.cells().length === 4", timeout=15000)
    playback = ctx.new_page()
    t0 = time.monotonic()
    playback.goto("/playback", timeout=15000)
    playback.wait_for_load_state("load", timeout=15000)
    assert time.monotonic() - t0 < 10
    time.sleep(1.5)
    assert site.subscribers <= 4, site.subscribers   # 1 por página del panel + 1 para los muros
    took = wall.evaluate("""async () => { const c = new AbortController(); setTimeout(() => c.abort(), 8000);
      const t = performance.now(); try { const r = await fetch('/api/cameras', {signal: c.signal,
        headers: {'X-Requested-With': 'vms'}}); return r.ok ? performance.now() - t : -1; } catch (e) { return -2; } }""")
    assert 0 <= took < 3000, took
