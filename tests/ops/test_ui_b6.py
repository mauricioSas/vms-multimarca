"""Interfaz de B6 en Chromium contra el backend REAL (criterio 9 y ayuda contextual).

- Asistente de primer uso completo (9 pasos) para un administrador sin equipos; nunca en `/wall/N`.
- Ayuda «?» en CADA sección de cada página (según `vms/web/static/help/es.json`), guías «Cómo hacer…» de las
  tareas pedidas y recorrido con Driver.js (copia local, sin CDN) solo para administradores.
- Secciones de estado, reproducción y analítica sin errores de JavaScript ni respuestas 4xx/5xx inesperadas.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fakes import FakeEngine
from tests.web.demo import ServerThread
from tests.web.real_backend import RealBackend
from tests.web.stub_backend import DEFAULT_USERS

pytestmark = pytest.mark.needs_browser
ROOT = Path(__file__).resolve().parents[2]
HELP = json.loads((ROOT / "vms" / "web" / "static" / "help" / "es.json").read_text(encoding="utf-8"))
ADMIN = ("admin", DEFAULT_USERS["admin"][0])
OPERATOR = ("operador", DEFAULT_USERS["operador"][0])
REQUIRED_TASKS = {"anadir-camara", "buscar-red", "asignar-monitor", "ver-grabaciones", "exportar-evidencia",
                  "dibujar-zonas", "leer-salud"}


CONTEXTS: list[Any] = []


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[RealBackend, ServerThread]]:
    backend = RealBackend(tmp_path / "datos", FakeEngine())
    srv = ServerThread(backend.app).start()
    try:
        yield backend, srv
    finally:
        while CONTEXTS:   # cerrar las páginas antes que el servidor (las conexiones SSE quedan abiertas)
            CONTEXTS.pop().close()
        srv.stop()


class Errors:
    def __init__(self, page: Any) -> None:
        self.items: list[str] = []
        page.on("pageerror", lambda exc: self.items.append(f"pageerror: {exc}"))
        page.on("console", lambda m: self.items.append(f"console: {m.text}") if m.type == "error" else None)
        page.on("response", lambda r: self.items.append(f"http {r.status} {r.request.method} {r.url}")
                if r.status >= 400 and r.status != 401 else None)

    def unexpected(self) -> list[str]:
        return [e for e in self.items if "401" not in e]


def _login(page: Any, user: tuple[str, str], path: str = "/") -> None:
    page.goto(f"/login?next={path}")
    page.fill("#username", user[0])
    page.fill("#password", user[1])
    page.click("#login-submit")
    page.wait_for_url(lambda url: "/login" not in url)
    # el muro mantiene abierta la conexión de eventos (SharedWorker de B2): ahí la red nunca queda en reposo
    page.wait_for_load_state("load" if path.startswith("/wall") else "networkidle")


def _api(srv: ServerThread, user: tuple[str, str] = ADMIN) -> httpx.Client:
    c = httpx.Client(base_url=srv.base_url, headers={"X-Requested-With": "vms"}, timeout=10)
    c.post("/api/auth/login", json={"username": user[0], "password": user[1]}).raise_for_status()
    return c


def _page(browser: Any, srv: ServerThread, **kw: Any) -> Any:
    ctx = browser.new_context(base_url=srv.base_url, viewport=kw.pop("viewport", {"width": 1500, "height": 950}),
                              locale="es-ES", timezone_id="Europe/Madrid", **kw)
    CONTEXTS.append(ctx)
    return ctx.new_page()


def _next(page: Any, expected: str) -> None:
    page.click("#ob-next")
    page.wait_for_function("t => document.querySelector('#ob-title')?.firstChild?.textContent.trim() === t", arg=expected)


def _title(page: Any) -> str:
    """Título del paso del asistente (sin el botón «?» de la ayuda)."""
    return str(page.eval_on_selector("#ob-title", "e => e.firstChild.textContent")).strip()


def test_first_run_wizard_complete(browser: Any, server: tuple[RealBackend, ServerThread]) -> None:
    backend, srv = server
    page = _page(browser, srv)
    errs = Errors(page)
    _login(page, ADMIN)
    page.wait_for_selector("#onboarding-root .ob-card")
    assert _title(page) == "Bienvenida"
    _next(page, "Buscar en la red")
    page.click("text=Escribir la IP a mano")
    page.wait_for_selector("#device-dialog[open]")
    page.click("#device-dialog [data-close] >> nth=0")
    # el instalador da de alta un equipo mientras sigue el asistente
    c = _api(srv)
    r = c.post("/api/devices", json={"name": "Cámara puerta", "vendor": "hikvision", "kind": "camera",
                                     "host": "127.0.0.1", "http_port": srv.port, "rtsp_port": 9, "username": "vms",
                                     "password": "Lectura#2026", "import_channels": "all"})
    r.raise_for_status()
    _next(page, "Usuario y contraseña")
    assert "solo lectura" in page.inner_text(".ob-body")
    _next(page, "Probar la conexión")
    page.click(".ob-devices button")
    page.wait_for_selector(".ob-diag .ops-diag-box", timeout=20000)
    assert page.locator(".ops-diag-step").count() == 11
    _next(page, "Elegir canales")
    _next(page, "Imagen de referencia")
    backend.app.state.ops.reference_interval_s = 0.0
    page.click(".ob-devices button[data-camera]")
    page.wait_for_selector("text=Referencia fijada", timeout=20000)
    _next(page, "Montar el muro")
    _next(page, "Días de grabación")
    page.fill(".ob-days", "45")
    assert "22.3" in page.inner_text(".ob-warn"), "aviso RGPD por encima de 30 días"
    page.fill(".ob-days", "30")
    page.click(".ob-row button")
    page.wait_for_selector("text=Días de grabación guardados")
    _next(page, "¡Listo!")
    page.click("text=Terminar")
    page.wait_for_selector("#onboarding-root", state="hidden")
    st = c.get("/api/onboarding/state").json()
    assert st["wizard_completed"] and not st["show_wizard"]
    ref = backend.paths.base / "ops" / "references"
    assert list(ref.glob("*-day.jpg")), "la referencia se fijó desde el asistente"
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.is_hidden("#onboarding-root"), "no vuelve a salir"
    assert errs.unexpected() == []


def test_wizard_never_on_walls_nor_for_operators(browser: Any, server: tuple[RealBackend, ServerThread]) -> None:
    _, srv = server
    page = _page(browser, srv)
    errs = Errors(page)
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    _login(page, ADMIN, "/wall/1")
    page.wait_for_timeout(800)
    assert page.locator("#onboarding-root, .help-btn, .help-global, .driver-popover, .ob-card").count() == 0
    assert not any("/vendor/driver.js" in u or "/api/onboarding" in u or "help/es.json" in u for u in requests)
    k = _page(browser, srv)
    k.goto("/api/auth/kiosk?token=kiosk-token-pruebas&next=/wall/2")
    k.wait_for_load_state("load")
    assert k.locator(".help-btn, .ob-card").count() == 0
    op = _page(browser, srv)
    _login(op, OPERATOR)
    assert op.is_hidden("#onboarding-root"), "el asistente es para quien configura"
    op.click(".help-global")
    op.wait_for_selector(".help-drawer:not([hidden])")
    assert op.locator("text=Ver recorrido").count() == 0 and op.locator("text=Recorridos de 1 minuto").count() == 0
    assert errs.unexpected() == []


@pytest.mark.parametrize("path", ["/", "/playback", "/analytics", "/status"])
def test_help_button_in_every_section(browser: Any, server: tuple[RealBackend, ServerThread], path: str) -> None:
    _, srv = server
    c = _api(srv)
    c.put("/api/onboarding/state", json={"wizard_completed": True})
    page = _page(browser, srv)
    errs = Errors(page)
    _login(page, ADMIN, path)
    keys = [k for k, s in HELP["sections"].items() if s["page"] == path]
    assert keys
    for k in keys:
        anchor = HELP["sections"][k]["anchor"]
        if k == "panel.wizard" or (k == "status.updates" and page.locator(anchor).count() == 0):
            continue
        page.wait_for_selector(f".help-btn[data-help='{k}']", state="attached", timeout=8000)
    first = keys[0]
    page.click(f".help-btn[data-help='{first}']")
    page.wait_for_selector(".help-drawer:not([hidden])")
    assert HELP["sections"][first]["title"] in page.inner_text("#help-title")
    assert HELP["sections"][first]["intro"][0][:40] in page.inner_text(".help-body")
    page.keyboard.press("Escape")
    page.keyboard.press("F1")
    page.wait_for_selector(".help-drawer:not([hidden])")
    body = page.inner_text(".help-body")
    for task in REQUIRED_TASKS:
        assert HELP["tasks"][task]["title"] in body, task
    assert errs.unexpected() == []


def test_help_texts_cover_required_tasks_and_buttons() -> None:
    assert REQUIRED_TASKS <= set(HELP["tasks"])
    for key, s in HELP["sections"].items():
        assert s["title"] and s["intro"] and s["page"] in ("/", "/playback", "/analytics", "/status"), key
        for b in s.get("buttons", []):
            assert b["label"] and len(b["text"]) > 15, (key, b)
    pages = (ROOT / "vms" / "web" / "pages").glob("*.html")
    html = {p.name: p.read_text(encoding="utf-8") for p in pages}
    # cada botón visible de las páginas está explicado en la ayuda de su sección
    for btn_id, key in {"btn-discover": "panel.page", "btn-add-device": "panel.page", "btn-add-camera": "panel.cameras",
                        "btn-save-wall": "panel.monitors", "btn-clear-wall": "panel.monitors",
                        "btn-open-wall": "panel.monitors", "btn-device-test": "panel.device_dialog",
                        "btn-channels-import": "panel.channels_dialog", "btn-discover-run": "panel.discover_dialog",
                        "clip-download": "playback.clip", "clip-from-position": "playback.clip",
                        "tool-line": "analytics.tools", "tool-zone": "analytics.tools", "an-snapshot": "analytics.tools",
                        "rule-redraw": "analytics.rule_form", "st-refresh": "status.page"}.items():
        page = next(n for n, t in html.items() if f'id="{btn_id}"' in t)
        assert HELP["sections"][key]["page"] == {"index.html": "/", "playback.html": "/playback",
                                                  "analytics.html": "/analytics", "status.html": "/status"}[page]
        assert HELP["sections"][key]["buttons"], key


def test_driver_tour_runs_locally_and_is_remembered(browser: Any, server: tuple[RealBackend, ServerThread]) -> None:
    _, srv = server
    c = _api(srv)
    c.put("/api/onboarding/state", json={"wizard_completed": True})
    page = _page(browser, srv)
    errs = Errors(page)
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    _login(page, ADMIN)
    page.click(".help-btn[data-help='panel.page']")
    page.click("text=Ver recorrido de 1 minuto")
    page.wait_for_selector(".driver-popover")
    assert "Buscar en la red" in page.inner_text(".driver-popover")
    page.click(".driver-popover-next-btn")
    assert "Añadir equipo" in page.inner_text(".driver-popover")
    page.keyboard.press("Escape")
    page.wait_for_selector(".driver-popover", state="detached")
    deadline = time.monotonic() + 5
    while "panel" not in c.get("/api/onboarding/state").json()["tours_seen"]:
        assert time.monotonic() < deadline, "el recorrido no quedó como visto"
        time.sleep(0.1)
    assert all(u.startswith(srv.base_url) for u in requests), "nada desde CDN"
    assert any(u.endswith("/vendor/driver.js/driver.js.mjs") for u in requests)
    assert errs.unexpected() == []


def test_status_playback_analytics_sections_render(browser: Any, server: tuple[RealBackend, ServerThread]) -> None:
    backend, srv = server
    c = _api(srv)
    c.put("/api/onboarding/state", json={"wizard_completed": True})
    dev = c.post("/api/devices", json={"name": "NVR cajas", "vendor": "hikvision", "kind": "nvr", "host": "192.0.2.10",
                                       "username": "vms", "password": "Lectura#2026", "import_channels": [1, 2]}).json()
    cam = dev["cameras"][0]
    page = _page(browser, srv)
    errs = Errors(page)
    _login(page, ADMIN, "/status")
    for hid in ("h-ops-health", "h-ops-clock", "h-ops-forecast", "h-ops-report", "h-ops-diagnose", "h-ops-security",
                "h-ops-notify"):
        page.wait_for_selector(f"#{hid}")
    page.wait_for_selector("#ops-health-table tbody tr[data-camera]")
    assert page.locator("#ops-health-table [data-status='unknown']").count() == 2, "sin referencia: «Sin datos»"
    page.wait_for_selector("text=Todavía no se ha hecho ninguna auditoría")
    page.wait_for_selector(".ops-notify-form")
    # reproducción: marcador en la posición actual y su marca en la línea de tiempo
    page.goto(f"/playback?camera={cam}")
    page.wait_for_selector("#h-ops-bookmarks")
    page.wait_for_selector("#timeline-spans i")
    page.evaluate("() => window.__vmsPlayback.play(window.__vmsPlayback.spans[0].start)")
    page.click("#bm-add")
    page.fill(".ops-modal input[aria-label='Nota']", "Discusión en caja 2")
    page.click("text=Guardar marcador")
    page.wait_for_selector(".ops-bookmarks tr[data-bookmark]")
    page.wait_for_selector("#timeline-track .tl-ev.tl-bookmark")
    page.wait_for_selector("#h-ops-evidence")
    page.wait_for_selector(".ops-evidence-form")
    # analítica: exportar conteos sin base de datos explica por qué
    page.goto("/analytics")
    page.wait_for_selector("#h-ops-counts")
    with page.expect_response(lambda r: "/api/analytics/counts.csv" in r.url) as resp:
        page.click("text=Descargar CSV")
    assert resp.value.status == 503
    page.wait_for_selector("text=VMS_PG_DSN")
    unexpected = [e for e in errs.unexpected() if "counts.csv" not in e and "503" not in e]
    assert unexpected == []


@pytest.mark.parametrize("path", ["/", "/playback", "/status", "/analytics"])
def test_b6_sections_fit_phone_width(browser: Any, server: tuple[RealBackend, ServerThread], path: str) -> None:
    _, srv = server
    c = _api(srv)
    c.post("/api/devices", json={"name": "NVR", "vendor": "hikvision", "kind": "nvr", "host": "192.0.2.10",
                                 "username": "vms", "password": "Lectura#2026", "import_channels": [1]})
    page = _page(browser, srv, viewport={"width": 390, "height": 844})
    errs = Errors(page)
    _login(page, ADMIN, path)
    page.wait_for_timeout(500)
    sw, cw = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert sw <= cw, f"{path}: scroll horizontal ({sw} > {cw})"
    assert errs.unexpected() == []
