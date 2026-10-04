"""Interfaz del panel central en un navegador real (Chromium de Playwright) contra uvicorn + PostgreSQL."""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.central.conftest import ADMIN_PW, NOW

STATIC = Path(__file__).resolve().parents[2] / "central" / "web" / "static"


@pytest.mark.parametrize("js", sorted(STATIC.glob("*.js")), ids=lambda p: p.name)
def test_javascript_syntax(js: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node no está instalado; la sintaxis se comprueba igualmente en la prueba con navegador")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.fixture
def live_central(central_settings, seeded_dsn) -> Iterator[str]:  # type: ignore[no-untyped-def]
    import uvicorn

    from central.app import create_app

    from pydantic import SecretStr

    settings = central_settings.model_copy(update={"pg_dsn": SecretStr(seeded_dsn)})
    app = create_app(settings, clock=lambda: NOW)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "el panel no arrancó"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        th.join(10)


@pytest.mark.needs_browser
@pytest.mark.needs_postgres
@pytest.mark.slow
def test_dashboard_site_report_and_admin_in_browser(live_central: str) -> None:
    pw_mod = pytest.importorskip("playwright.sync_api")
    errors: list[str] = []
    with pw_mod.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # Chromium no instalado en el venv
            pytest.skip(f"No se pudo abrir Chromium: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(live_central + "/")
            assert page.url.endswith("/login")
            page.fill("#username", "admin")
            page.fill("#password", "mala-clave")
            page.click("button[type=submit]")
            page.wait_for_selector("#msg.show")
            assert "incorrectos" in page.inner_text("#msg")
            page.fill("#password", ADMIN_PW)
            page.click("button[type=submit]")
            page.wait_for_url(live_central + "/")
            errors.clear()  # el 401 del intento fallido es esperado
            page.wait_for_selector("#sites tbody tr")
            rows = page.locator("#sites tbody tr")
            assert rows.count() == 3
            body = page.inner_text("#sites")
            assert "Caída" in body and "Con avisos" in body and "Sin datos" in body
            page.wait_for_selector("#compare svg")
            assert "Tienda Gràcia" in (page.text_content("#compare") or "")
            page.wait_for_selector("#queues table")
            assert "Cola cajas" in page.inner_text("#queues")

            page.click("text=Tienda Gràcia")
            page.wait_for_selector("#alerts tbody tr")
            assert page.locator("#counts svg .bar").count() >= 2
            assert "En curso" in page.inner_text("#alerts")
            page.click("#range button[data-range=week]")
            # (la CSP del panel prohíbe eval, así que se espera desde Python y no con wait_for_function)
            for _ in range(100):
                if "18 en total" in page.inner_text("#counts-title"):
                    break
                page.wait_for_timeout(50)
            assert "18 en total" in page.inner_text("#counts-title")

            page.click("text=2026-09-28")
            page.wait_for_selector("#body h3")
            assert "1234" in page.inner_text("#body")

            page.goto(live_central + "/admin")
            page.fill("#site-id", "site-bcn-001")
            page.click("#token-form button")
            page.wait_for_selector("#token-out .token")
            assert page.inner_text("#token-out .token").startswith("vms_")
        finally:
            browser.close()
    assert errors == []
