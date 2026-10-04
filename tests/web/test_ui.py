"""Pruebas de la interfaz en Chromium (Playwright) contra el backend de pruebas con FakeEngine.

Cubren el panel (alta de equipos, búsqueda, importación de canales, asignación a monitores),
el muro (estados, ampliar celda, cambio de layout sin recargar), la analítica (dibujo de línea
y zona), reproducción (línea de tiempo), estado, permisos y kiosco. El vídeo real por WebRTC se
prueba en tests/e2e/test_web_e2e.py.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import get_free_port
from tests.web.conftest import ADMIN, OPERATOR, PageErrors, login
from tests.web.demo import ServerThread
from tests.web.stub_backend import StubBackend

pytestmark = [pytest.mark.needs_browser, pytest.mark.slow]

ALLOWED_CONSOLE = ("401 (Unauthorized)",)


def api_client(server: ServerThread, user: tuple[str, str] = ADMIN) -> httpx.Client:
    c = httpx.Client(base_url=server.base_url, headers={"X-Requested-With": "vms"}, timeout=10)
    c.post("/api/auth/login", json={"username": user[0], "password": user[1]}).raise_for_status()
    return c


def add_nvr(c: httpx.Client, name: str = "NVR cajas", channels: Any = "all") -> dict[str, Any]:
    r = c.post("/api/devices", json={"name": name, "vendor": "hikvision", "kind": "nvr", "host": "192.168.1.20",
                                     "username": "admin", "password": "secreto", "import_channels": channels})
    r.raise_for_status()
    return r.json()


@pytest.fixture
def stub(fake_stub: tuple[StubBackend, ServerThread]) -> tuple[StubBackend, ServerThread]:
    backend, _ = fake_stub
    # que el WHEP del FakeEngine apunte a un puerto cerrado (y no a un 8889 que pudiera existir)
    dead = get_free_port()
    backend.engine.whep_url = lambda cid, stream: f"http://127.0.0.1:{dead}/{cid}/{stream}/whep"  # type: ignore[method-assign]
    return fake_stub


# ====================================================================== autenticación
def test_login_flow_and_redirect(stub: Any, make_context: Any) -> None:
    _, server = stub
    page = make_context(server.base_url).new_page()
    errors = PageErrors().attach(page)
    page.goto("/wall/1")
    page.wait_for_url(re.compile(r"/login\?next=(%2F|/)wall(%2F|/)1$"))
    page.fill("#username", "admin")
    page.fill("#password", "mala-contraseña")
    page.click("#login-submit")
    page.wait_for_selector("#login-error:not([hidden])")
    assert "incorrectos" in page.inner_text("#login-error")
    page.fill("#password", ADMIN[1])
    page.click("#login-submit")
    page.wait_for_url(lambda url: url.endswith("/wall/1") and "/login" not in url)
    # un next externo no se sigue (redirección abierta)
    page.goto("/login?next=//evil.example/x")
    page.wait_for_url(f"{server.base_url}/")
    assert errors.unexpected(ALLOWED_CONSOLE) == []


def test_login_rate_limit_message(stub: Any, make_context: Any) -> None:
    _, server = stub
    page = make_context(server.base_url).new_page()
    page.goto("/login")
    for _ in range(6):
        page.fill("#username", "admin")
        page.fill("#password", "fallo-fallo")
        page.click("#login-submit")
        page.wait_for_selector("#login-error:not([hidden])")
    assert "Demasiados intentos" in page.inner_text("#login-error")


# ====================================================================== panel
def test_add_device_and_assign_to_monitor(stub: Any, make_context: Any, screenshots_dir: Path) -> None:
    backend, server = stub
    page = make_context(server.base_url).new_page()
    errors = PageErrors().attach(page)
    login(page)
    page.click("#btn-add-device")
    page.fill("#dev-name", "NVR cajas")
    page.select_option("#dev-vendor", "hikvision")
    page.select_option("#dev-kind", "nvr")
    page.fill("#dev-host", "192.168.1.20")
    page.fill("#dev-password", "Clave#Segura:1")
    assert "/Streaming/Channels/101" in page.inner_text("#paths-preview")
    page.click("#btn-device-test")
    page.wait_for_selector("#test-channels .channel-item")
    assert "Conexión correcta" in page.inner_text("#device-test-result")
    # el canal 3 del equipo falso no tiene vídeo: viene desmarcado
    checked = page.eval_on_selector_all("#test-channels input", "els => els.map(e => e.checked)")
    assert checked == [True, True, False, True]
    page.screenshot(path=str(screenshots_dir / "panel-alta-equipo.png"))
    page.click("#btn-device-save")
    page.wait_for_selector("#cameras-table tbody tr[data-camera] >> nth=2")
    assert page.locator("#cameras-table tbody tr[data-camera]").count() == 3
    assert page.locator("#devices-table tbody tr[data-device]").count() == 1

    c = api_client(server)
    dev = c.get("/api/devices").json()[0]
    assert dev["has_password"] is True and "password" not in dev
    assert backend.creds.get_device_password(dev["id"]) == "Clave#Segura:1"
    cams = c.get("/api/cameras").json()
    assert sorted(x["channel"] for x in cams) == [1, 2, 4]

    # monitor 2: celda 1 con la lista, celda 2 arrastrando, distribución 9
    page.click("#tab-monitor-2")
    page.click("#grid-buttons [data-grid='9']")
    page.select_option("[data-slot-select='0']", cams[0]["id"])
    page.drag_and_drop(f".cam-chip[data-camera='{cams[1]['id']}']", "[data-slot='1']")
    page.fill("#wall-name", "Cajas")
    assert page.is_visible("#wall-dirty")
    page.click("#btn-save-wall")
    page.wait_for_selector("#wall-dirty", state="hidden")
    wall = c.get("/api/walls/2").json()
    assert wall["grid"] == 9 and wall["name"] == "Cajas"
    assert wall["cells"][:3] == [cams[0]["id"], cams[1]["id"], None]
    assert page.locator(".monitor-screen .slot").count() == 9
    page.screenshot(path=str(screenshots_dir / "panel.png"), full_page=True)
    assert errors.unexpected(ALLOWED_CONSOLE) == []


def test_vendor_selector_fills_paths(stub: Any, make_context: Any) -> None:
    _, server = stub
    page = make_context(server.base_url).new_page()
    login(page)
    page.click("#btn-add-device")
    page.select_option("#dev-vendor", "dahua")
    assert "/cam/realmonitor?channel=1&subtype=0" in page.inner_text("#paths-preview")
    assert "/cam/realmonitor?channel=1&subtype=1" in page.inner_text("#paths-preview")
    assert not page.is_visible("#dev-main-path")
    page.select_option("#dev-vendor", "onvif")
    assert "ONVIF" in page.inner_text("#paths-preview")
    page.select_option("#dev-vendor", "generic")
    assert page.is_visible("#dev-main-path")
    # cámara genérica con ruta manual: crea equipo + cámara con esa ruta
    page.select_option("#dev-kind", "camera")
    page.fill("#dev-name", "Cámara almacén")
    page.fill("#dev-host", "10.0.0.50")
    page.fill("#dev-main-path", "/live/ch00_0")
    page.click("#btn-device-save")
    page.wait_for_selector("#cameras-table tbody tr[data-camera]")
    cam = api_client(server).get("/api/cameras").json()[0]
    assert cam["main_path"] == "/live/ch00_0" and cam["has_sub"] is False


def test_device_form_validation_errors(stub: Any, make_context: Any) -> None:
    _, server = stub
    page = make_context(server.base_url).new_page()
    login(page)
    page.click("#btn-add-device")
    page.fill("#dev-name", "Malo")
    page.fill("#dev-host", "rtsp://192.168.1.300/stream")
    page.click("#btn-device-save")
    page.wait_for_selector(".field-error")
    assert page.get_attribute("#dev-host", "aria-invalid") == "true"
    assert "sin rtsp://" in page.inner_text(".field-error")


def test_discovery_prefills_device_form(stub: Any, make_context: Any) -> None:
    _, server = stub
    page = make_context(server.base_url).new_page()
    login(page)
    page.click("#btn-discover")
    page.click("#btn-discover-run")
    page.wait_for_selector("#discover-table [data-add]")
    assert page.locator("#discover-table tbody tr").count() == 2
    page.click("#discover-table [data-add] >> nth=1")
    assert page.input_value("#dev-host") == "192.168.1.108"
    assert page.input_value("#dev-vendor") == "dahua"
    assert page.input_value("#dev-kind") == "nvr"


def test_import_channels_dialog(stub: Any, make_context: Any) -> None:
    _, server = stub
    c = api_client(server)
    dev = add_nvr(c, channels=[1])
    page = make_context(server.base_url).new_page()
    login(page)
    page.click(f"#devices-table tr[data-device='{dev['id']}'] [data-act='channels']")
    page.wait_for_selector("#channels-list .channel-item")
    # el canal 1 ya está dado de alta: aparece deshabilitado
    assert page.is_disabled("#channels-list input[value='1']")
    page.click("#channels-none")
    page.check("#channels-list input[value='2']")
    page.check("#channels-list input[value='3']")
    page.click("#btn-channels-import")
    page.wait_for_selector("#channels-dialog", state="hidden")
    assert sorted(x["channel"] for x in c.get("/api/cameras").json()) == [1, 2, 3]


def test_delete_camera_cascades_in_ui(stub: Any, make_context: Any) -> None:
    _, server = stub
    c = api_client(server)
    add_nvr(c, channels=[1, 2])
    cams = c.get("/api/cameras").json()
    c.put("/api/walls/1", json={"cells": [cams[0]["id"], cams[1]["id"]]}).raise_for_status()
    page = make_context(server.base_url).new_page()
    login(page)
    page.click(f"#cameras-table tr[data-camera='{cams[0]['id']}'] [data-act='delete']")
    page.click("dialog[open] button[value='yes']")
    page.wait_for_function("document.querySelectorAll('#cameras-table tr[data-camera]').length === 1")
    assert c.get("/api/walls/1").json()["cells"][:2] == [None, cams[1]["id"]]


# ====================================================================== muro
def test_wall_layout_states_expand_and_live_update(stub: Any, make_context: Any, screenshots_dir: Path) -> None:
    _, server = stub
    c = api_client(server)
    add_nvr(c, channels=[1, 2])
    cams = c.get("/api/cameras").json()
    c.put("/api/walls/1", json={"name": "Entrada", "grid": 4, "cells": [cams[0]["id"], None, cams[1]["id"]]}).raise_for_status()

    page = make_context(server.base_url, viewport={"width": 1280, "height": 720}).new_page()
    login(page, next_path="/wall/1")
    page.wait_for_selector(".cell >> nth=3")
    assert page.locator(".cell").count() == 4
    labels = page.eval_on_selector_all(".cell .label", "els => els.map(e => e.textContent)")
    assert labels[0] == cams[0]["name"] and labels[2] == cams[1]["name"]
    assert page.inner_text(".cell[data-index='1'] .notice strong") == "Sin cámara"
    # sin MediaMTX el lector WHEP no conecta: la celda pasa a «Sin señal» o «Reconectando», nunca se queda colgada
    page.wait_for_function("""() => ['offline', 'reconnecting'].includes(
        document.querySelector(".cell[data-index='0']").dataset.state)""", timeout=15000)
    # sin barras de scroll
    dims = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth,"
                         " document.documentElement.scrollHeight, document.documentElement.clientHeight]")
    assert dims[0] <= dims[1] and dims[2] <= dims[3]
    page.screenshot(path=str(screenshots_dir / "muro-sin-senal.png"))

    # doble clic amplía con el flujo principal; Escape vuelve
    page.dblclick(".cell[data-index='0']")
    page.wait_for_selector(".wall.expanded .cell.is-expanded")
    assert page.evaluate("() => window.__vmsWall.cells().find(c => c.index === 0).stream") == "main"
    assert page.locator(".cell:visible").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_selector(".wall:not(.expanded)")
    assert page.evaluate("() => window.__vmsWall.cells().find(c => c.index === 0).stream") == "sub"

    # cambio de layout desde otro sitio: el muro se actualiza solo, sin recargar la página
    page.evaluate("() => { window.__noReload = true; }")
    c.put("/api/walls/1", json={"grid": 9}).raise_for_status()
    page.wait_for_function("() => document.querySelectorAll('.cell').length === 9", timeout=10000)
    assert page.evaluate("() => window.__noReload") is True
    # las celdas que no cambiaron no se recrean ni dejan conexiones de más
    open_pcs = page.evaluate("() => window.__pcTracker.open")
    assert open_pcs <= 2


def test_wall_layout_buttons_for_operator(stub: Any, make_context: Any) -> None:
    _, server = stub
    c = api_client(server)
    add_nvr(c, channels=[1])
    page = make_context(server.base_url).new_page()
    login(page, user=OPERATOR, next_path="/wall/3")
    page.mouse.move(300, 300)
    page.click("#layout-buttons [data-grid='16']")
    page.wait_for_function("() => document.querySelectorAll('.cell').length === 16")
    assert c.get("/api/walls/3").json()["grid"] == 16


def test_kiosk_wall_is_read_only(stub: Any, make_context: Any) -> None:
    backend, server = stub
    page = make_context(server.base_url).new_page()
    page.goto(f"/api/auth/kiosk?token={backend.opt.kiosk_token}&next=/wall/2")
    page.wait_for_url("**/wall/2")
    page.wait_for_selector(".cell")
    assert page.is_hidden("#layout-buttons")
    assert page.is_hidden("#link-panel")
    # el kiosco no puede abrir el panel: vuelve al muro
    page.goto("/")
    page.wait_for_url("**/wall/1")


# ====================================================================== analítica
def _canvas_click(page: Any, fx: float, fy: float) -> None:
    box = page.locator("#an-canvas").bounding_box()
    page.mouse.click(box["x"] + fx * box["width"], box["y"] + fy * box["height"])


def test_analytics_draw_line_and_zone(stub: Any, make_context: Any, screenshots_dir: Path) -> None:
    _, server = stub
    c = api_client(server)
    add_nvr(c, channels=[1])
    cam = c.get("/api/cameras").json()[0]
    page = make_context(server.base_url).new_page()
    errors = PageErrors().attach(page)
    login(page, next_path="/analytics")
    page.wait_for_function("() => document.querySelector('#an-image').naturalWidth > 0")

    # línea de puerta de izquierda a derecha: la entrada apunta hacia abajo (n = (-v.y, v.x))
    page.click("#tool-line")
    _canvas_click(page, 0.2, 0.6)
    _canvas_click(page, 0.8, 0.6)
    page.wait_for_selector("#rule-form:not([hidden])")
    page.fill("#rule-name", "Puerta principal")
    page.click("#rule-save")
    page.wait_for_selector("#an-rules [data-rule]")
    rules = c.get(f"/api/analytics/rules?camera_id={cam['id']}").json()
    assert len(rules) == 1 and rules[0]["kind"] == "line"
    (sx, sy), (ex, ey) = rules[0]["start"], rules[0]["end"]
    assert abs(sx - 0.2) < 0.01 and abs(sy - 0.6) < 0.01 and abs(ex - 0.8) < 0.01 and abs(ey - 0.6) < 0.01
    assert rules[0]["invert"] is False

    # invertir sentido y guardar
    page.check("#rule-invert")
    page.click("#rule-save")
    page.wait_for_function("() => window.__vmsAnalytics.rules[0] && window.__vmsAnalytics.rules[0].invert === true")

    # zona de cola: 4 vértices y cierre en el primero
    page.click("#tool-zone")
    for fx, fy in ((0.55, 0.15), (0.9, 0.15), (0.9, 0.45), (0.55, 0.45)):
        _canvas_click(page, fx, fy)
    _canvas_click(page, 0.55, 0.15)
    page.wait_for_selector("#rule-form:not([hidden]) #rule-threshold")
    page.fill("#rule-name", "Cola cajas 1-3")
    page.fill("#rule-threshold", "4")
    page.fill("#rule-min-seconds", "45")
    page.fill("#rule-clear", "2")
    page.click("#rule-save")
    page.wait_for_function("() => window.__vmsAnalytics.rules.length === 2")
    zone = next(r for r in c.get("/api/analytics/rules").json() if r["kind"] == "zone")
    assert len(zone["polygon"]) == 4
    assert zone["alert_threshold"] == 4 and zone["alert_min_seconds"] == 45 and zone["clear_below"] == 2
    assert all(0 <= x <= 1 and 0 <= y <= 1 for x, y in zone["polygon"])

    # ajustes de la cámara
    page.check("#an-enabled")
    page.fill("#an-fps", "12")
    page.click("#an-cam-save")
    # No vale esperar a «un» .toast.ok: puede seguir visible el de una acción anterior. Se espera a
    # que el PUT haya llegado de verdad al backend.
    deadline = time.monotonic() + 10.0
    a: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        a = c.get("/api/analytics/cameras").json()
        if a and a[0].get("enabled") is True and a[0].get("fps") == 12.0:
            break
        time.sleep(0.1)
    assert a == [{**a[0], "camera_id": cam["id"], "enabled": True, "fps": 12.0}]
    page.screenshot(path=str(screenshots_dir / "analitica.png"), full_page=True)

    # validación: el fin del aviso debe ser menor que el umbral
    page.fill("#rule-clear", "9")
    page.click("#rule-save")
    assert "menor" in page.inner_text("#rule-error")
    assert errors.unexpected(ALLOWED_CONSOLE) == []


def test_line_direction_matches_backend_semantics(stub: Any, make_context: Any) -> None:
    """La flecha de «entrada» que dibuja la interfaz apunta al lado s > 0 de LineRule."""
    _, server = stub
    page = make_context(server.base_url).new_page()
    page.goto("/login")
    result = page.evaluate("""async () => {
      const g = await import('/static/js/geometry.js');
      const cases = [[[0.2, 0.6], [0.8, 0.6]], [[0.5, 0.1], [0.5, 0.9]], [[0.9, 0.2], [0.1, 0.7]]];
      return cases.map(([s, e]) => {
        const [dx, dy] = g.entryDirectionPx(s, e, 1280, 720, false);
        const mid = [(s[0] + e[0]) / 2, (s[1] + e[1]) / 2];
        const p = [mid[0] + dx * 0.01, mid[1] + dy * 0.01];
        const [ix, iy] = g.entryDirectionPx(s, e, 1280, 720, true);
        const q = [mid[0] + ix * 0.01, mid[1] + iy * 0.01];
        return [g.lineSide(s, e, p), g.lineSide(s, e, q)];
      });
    }""")
    for pos, neg in result:
        assert pos > 0 and neg < 0
    from vms.core.models import LineRule
    doc = LineRule.__doc__ or ""
    assert "s(p) = v.x * (p.y - start.y) - v.y * (p.x - start.x)" in doc


# ====================================================================== reproducción y estado
def test_playback_timeline_and_click(stub: Any, make_context: Any, screenshots_dir: Path) -> None:
    _, server = stub
    c = api_client(server)
    add_nvr(c, channels=[1])
    cam = c.get("/api/cameras").json()[0]
    page = make_context(server.base_url).new_page()
    login(page, next_path=f"/playback?camera={cam['id']}")
    page.wait_for_function("() => window.__vmsPlayback.spans.length === 2")
    assert page.locator("#timeline-spans i").count() == 2
    span = page.evaluate("() => window.__vmsPlayback.spans[0]")
    view = page.evaluate("() => window.__vmsPlayback.view")
    from datetime import datetime
    t0, t1 = (datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() for v in (view["from"], view["to"]))
    s0, s1 = (datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() for v in (span["start"], span["end"]))
    target = s0 + (s1 - s0) * 0.5
    box = page.locator("#timeline-track").bounding_box()
    with page.expect_request(re.compile(r"/api/recordings/.+/video\?start=")) as req:
        page.mouse.click(box["x"] + (target - t0) / (t1 - t0) * box["width"], box["y"] + box["height"] / 2)
    url = httpx.URL(req.value.url)
    start = datetime.fromisoformat(url.params["start"].replace("Z", "+00:00")).timestamp()
    assert s0 <= start < s1 and abs(start - target) < 120   # un píxel del día ≈ 70 s
    assert float(url.params["duration"]) <= 3600
    # el FakeEngine no tiene vídeo real: la página lo explica en vez de quedarse en negro
    page.wait_for_selector("#pb-overlay:not([hidden])")
    assert "No se pudo reproducir" in page.inner_text("#pb-overlay")
    href = page.get_attribute("#clip-download", "href")
    assert "download=1" in href and "format=mp4" in href
    page.screenshot(path=str(screenshots_dir / "reproduccion-sin-video.png"), full_page=True)


def test_status_page(stub: Any, make_context: Any, screenshots_dir: Path) -> None:
    _, server = stub
    c = api_client(server)
    add_nvr(c, channels=[1, 2])
    page = make_context(server.base_url).new_page()
    errors = PageErrors().attach(page)
    login(page, next_path="/status")
    page.wait_for_selector("#st-cams-table tbody tr td .pill")
    assert page.locator("#st-cams-table tbody tr").count() == 2
    assert page.inner_text("#st-engine") == "En marcha"
    assert "40" in page.inner_text("#st-disk")
    page.screenshot(path=str(screenshots_dir / "estado.png"), full_page=True)
    assert errors.unexpected(ALLOWED_CONSOLE) == []


# ====================================================================== permisos y diseño
def test_operator_sees_no_admin_controls(stub: Any, make_context: Any) -> None:
    _, server = stub
    add_nvr(api_client(server), channels=[1])
    page = make_context(server.base_url).new_page()
    login(page, user=OPERATOR)
    page.wait_for_selector("#cameras-table tr[data-camera]")
    assert page.is_hidden("#btn-add-device")
    assert page.is_hidden("#cameras-table [data-act='delete']")
    assert page.locator(".nav a[href='/analytics']").count() == 0
    assert page.is_visible("#btn-save-wall")


@pytest.mark.parametrize("path", ["/", "/playback", "/status", "/analytics", "/login", "/setup"])
def test_pages_fit_phone_width(stub: Any, make_context: Any, path: str) -> None:
    _, server = stub
    add_nvr(api_client(server), channels=[1])
    ctx = make_context(server.base_url, viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    errors = PageErrors().attach(page)
    if path not in ("/login", "/setup"):
        login(page, next_path=path)
    else:
        page.goto(path)
    page.wait_for_load_state("networkidle")
    sw, cw = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw, f"{path}: scroll horizontal ({sw} > {cw})"
    assert errors.unexpected(ALLOWED_CONSOLE) == []
