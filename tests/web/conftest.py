"""Fixtures de las pruebas de la interfaz web (navegador Playwright + backend de pruebas)."""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine
from tests.web.demo import ServerThread
from tests.web.stub_backend import DEFAULT_USERS, StubBackend, StubOptions, fake_discover

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "tests" / "e2e" / "screenshots"
ADMIN = ("admin", DEFAULT_USERS["admin"][0])
OPERATOR = ("operador", DEFAULT_USERS["operador"][0])

# Captura los RTCPeerConnection que crea la página para comprobar que no quedan conexiones vivas
# de más (fugas) tras reconexiones y cambios de layout.
PC_TRACKER = """
(() => {
  const Orig = window.RTCPeerConnection;
  if (!Orig || window.__pcTracker) return;
  const live = new Set();
  window.__pcTracker = { created: 0, get open() { return [...live].filter(p => p.connectionState !== 'closed').length; } };
  window.RTCPeerConnection = function (...args) {
    const pc = new Orig(...args);
    window.__pcTracker.created++;
    live.add(pc);
    const close = pc.close.bind(pc);
    pc.close = () => { live.delete(pc); return close(); };
    return pc;
  };
  window.RTCPeerConnection.prototype = Orig.prototype;
})();
"""


@pytest.fixture(scope="module")
def pw_browser() -> Iterator[Any]:
    """Playwright + Chromium por MÓDULO de pruebas, cerrado al terminar el módulo.

    El API síncrono de Playwright deja registrado un bucle de eventos en el hilo principal mientras
    vive; si se dejara abierto hasta el final de la sesión, todas las pruebas asíncronas que pytest
    ejecuta después fallarían con «Runner.run() cannot be called from a running event loop».
    Cerrándolo por módulo, el resto de la batería no se entera.
    """
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright no está instalado en el venv")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    except Exception as exc:  # noqa: BLE001 - Playwright lanza su propio Error genérico
        pw.stop()
        pytest.skip(f"Chromium de Playwright no disponible: {exc}")
    try:
        yield browser
    finally:
        try:
            browser.close()
        except Exception as exc:  # noqa: BLE001 - al cerrar solo se informa
            print(f"Aviso al cerrar Chromium: {exc}")
        pw.stop()


@pytest.fixture
def screenshots_dir() -> Path:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    return SCREENSHOTS


class PageErrors:
    """Recoge errores de JavaScript y de consola de una página."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def attach(self, page: Any) -> "PageErrors":
        page.on("pageerror", lambda exc: self.errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: self.errors.append(f"console.{msg.type}: {msg.text}")
                if msg.type == "error" else None)
        # los 5xx con su URL, para saber qué petición falló (la consola solo dice «500»)
        page.on("response", lambda r: self.errors.append(f"http {r.status} {r.request.method} {r.url}")
                if r.status >= 500 else None)
        return self

    def unexpected(self, allowed: tuple[str, ...] = ()) -> list[str]:
        # los 401 previstos (página de login) y los fallos de recursos que el test provoca a propósito
        # se filtran con `allowed`
        return [e for e in self.errors if not any(a in e for a in allowed)]


@pytest.fixture
def make_context(pw_browser: Any) -> Iterator[Callable[..., Any]]:
    contexts: list[Any] = []

    def make(base_url: str, **kwargs: Any) -> Any:
        ctx = pw_browser.new_context(base_url=base_url, viewport=kwargs.pop("viewport", {"width": 1600, "height": 900}),
                                     locale="es-ES", timezone_id="Europe/Madrid", **kwargs)
        ctx.add_init_script(PC_TRACKER)
        contexts.append(ctx)
        return ctx

    yield make
    for ctx in contexts:
        ctx.close()


def login(page: Any, user: tuple[str, str] = ADMIN, next_path: str = "/") -> None:
    page.goto(f"/login?next={next_path}")
    page.fill("#username", user[0])
    page.fill("#password", user[1])
    page.click("#login-submit")
    page.wait_for_url(lambda url: "/login" not in url and url.split("://", 1)[-1].split("/", 1)[-1] == next_path.lstrip("/"))
    page.wait_for_load_state("load")


def backend_class() -> Any:
    """StubBackend por defecto; con VMS_TEST_WEB_BACKEND=real, el backend real (vms.api)."""
    if os.environ.get("VMS_TEST_WEB_BACKEND", "").lower() == "real":
        from tests.web.real_backend import RealBackend
        return RealBackend
    return StubBackend


@pytest.fixture
def fake_stub(tmp_path: Path) -> Iterator[tuple[StubBackend, ServerThread]]:
    """Backend (de pruebas o real) con FakeEngine (sin vídeo real) servido en un puerto libre."""
    engine = FakeEngine()
    backend = backend_class()(tmp_path / "data", engine, StubOptions(discover=fake_discover, status_interval=1.0))
    server = ServerThread(backend.app).start()
    try:
        yield backend, server
    finally:
        server.stop()
