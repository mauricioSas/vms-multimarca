"""Panel central: versiones por sede (B4, CONTRATO §15.7)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from central.updates import directive_for

from .conftest import CSRF, NOW, login

pytestmark = pytest.mark.needs_postgres


async def test_list_and_change_channel_hold_window(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.get("/api/updates/sites")
    assert r.status_code == 200
    sites = {s["site_id"]: s for s in r.json()}
    assert sites["site-bcn-001"]["installed"] == "0.1.0" and sites["site-bcn-001"]["channel"] == "stable"
    assert sites["site-can-003"]["last_seen"] is None
    r = await admin_client.put("/api/updates/sites/site-bcn-001", json={"channel": "pilot", "hold": True,
                                                                       "window": "02:00-04:00"}, headers=CSRF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "pilot" and body["hold"] is True and body["window"] == "02:00-04:00"
    assert body["updated_by"] == "admin"
    bad = await admin_client.put("/api/updates/sites/site-bcn-001", json={"channel": "Pilot!"}, headers=CSRF)
    assert bad.status_code == 422
    bad = await admin_client.put("/api/updates/sites/site-bcn-001", json={"window": "25:00-03:00"}, headers=CSRF)
    assert bad.status_code == 422
    bad = await admin_client.put("/api/updates/sites/site-bcn-001", json={"version": "9.9.9"}, headers=CSRF)
    assert bad.status_code == 422                                     # el panel no elige versiones
    assert (await admin_client.put("/api/updates/sites/site-xxx-999", json={"hold": True},
                                   headers=CSRF)).status_code == 404


async def test_rollback_and_check_requests(admin_client: httpx.AsyncClient, seeded_dsn: str) -> None:
    r = await admin_client.post("/api/updates/sites/site-mad-002/rollback", json={"to": None}, headers=CSRF)
    assert r.status_code == 200 and r.json()["rollback_to"] == "previous"
    r = await admin_client.post("/api/updates/sites/site-mad-002/rollback", json={"to": "no-version"}, headers=CSRF)
    assert r.status_code == 422
    r = await admin_client.post("/api/updates/sites/site-mad-002/check", headers=CSRF)
    assert r.status_code == 200 and r.json()["check_requested_at"]
    async with await psycopg.AsyncConnection.connect(seeded_dsn, autocommit=True, row_factory=dict_row) as c:
        d = await directive_for(c, "site-mad-002", NOW + timedelta(minutes=1))
        assert d is not None and d["update"]["rollback_to"] == "previous" and d["update"]["check"] is True
        # la sede informa por el latido: se copia en site_versions
        d2 = await directive_for(c, "site-mad-002", NOW + timedelta(minutes=2),
                                 {"installed": "2.1.0", "state": "good", "last_result": "update_ok",
                                  "message_es": "Actualizado", "secreto": "no"})
        assert d2 is not None
        row = await (await c.execute("SELECT installed, last_result FROM site_versions WHERE site_id = %s",
                                     ("site-mad-002",))).fetchone()
        assert row == {"installed": "2.1.0", "last_result": "update_ok"}
        # una petición de vuelta atrás caduca a las 24 h
        d3 = await directive_for(c, "site-mad-002", NOW + timedelta(hours=25))
        assert d3 is not None and d3["update"]["rollback_to"] is None
        assert await directive_for(c, "site-can-003", NOW) is None             # sin nada pedido
    r = await admin_client.delete("/api/updates/sites/site-mad-002/rollback", headers=CSRF)
    assert r.status_code == 200 and r.json()["rollback_to"] is None


async def test_operator_can_read_but_not_change(client_factory: Callable[..., httpx.AsyncClient],
                                                admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.post("/api/users", json={"username": "operador", "password": "operador-123",
                                                    "role": "operator"}, headers=CSRF)
    assert r.status_code in (200, 201), r.text
    op = client_factory()
    await login(op, "operador", "operador-123")
    assert (await op.get("/api/updates/sites")).status_code == 200
    assert (await op.put("/api/updates/sites/site-bcn-001", json={"hold": True}, headers=CSRF)).status_code == 403
    assert (await op.post("/api/updates/sites/site-bcn-001/rollback", json={}, headers=CSRF)).status_code == 403
    anon = client_factory()
    assert (await anon.get("/api/updates/sites")).status_code == 401


async def test_versions_page(client_factory: Callable[..., httpx.AsyncClient], admin_client: httpx.AsyncClient) -> None:
    anon = client_factory()
    r = await anon.get("/updates")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await admin_client.get("/updates")
    assert r.status_code == 200 and "Versiones por sede" in r.text and "/static/updates.js" in r.text
    js = await admin_client.get("/static/updates.js")
    assert js.status_code == 200 and "api/updates/sites" in js.text


def test_site_versions_table_has_row_security(seeded_dsn: str) -> None:
    with psycopg.connect(seeded_dsn) as c:
        row: Any = c.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'site_versions'").fetchone()
        assert row is not None and row[0] is True
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO site_versions (site_id, channel) VALUES ('site-bcn-001', 'NO VALE')")


@pytest.mark.needs_browser
@pytest.mark.slow
def test_versions_page_in_browser(central_settings: Any, seeded_dsn: str) -> None:
    """La página «Versiones» carga sin errores, filtra y el administrador retiene una sede."""
    import socket
    import threading
    import time

    import uvicorn
    from pydantic import SecretStr

    from central.app import create_app

    from .conftest import ADMIN_PW

    pw_mod = pytest.importorskip("playwright.sync_api")
    app = create_app(central_settings.model_copy(update={"pg_dsn": SecretStr(seeded_dsn)}), clock=lambda: NOW)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    errors: list[str] = []
    try:
        with pw_mod.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"No se pudo abrir Chromium: {exc}")
            try:
                page = browser.new_page(viewport={"width": 1366, "height": 900})
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base + "/login?next=/updates")
                page.fill("#username", "admin")
                page.fill("#password", ADMIN_PW)
                page.click("button[type=submit]")
                page.wait_for_url(base + "/updates")
                page.wait_for_selector(".upd-table tbody tr")
                assert page.locator(".upd-table tbody tr").count() == 4
                page.fill("#filter", "Gràcia")
                assert page.locator(".upd-table tbody tr").count() == 1
                page.click(".upd-table tbody tr >> text=Retener")
                page.wait_for_selector(".upd-table tbody tr >> text=Reanudar")
                assert "Retenida" in page.inner_text(".upd-table tbody tr")
                page.select_option("#only", "held")
                assert page.locator(".upd-table tbody tr").count() == 1
            finally:
                browser.close()
    finally:
        server.should_exit = True
        th.join(10)
    assert errors == []
