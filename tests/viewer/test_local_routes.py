"""Router `local` del visor (CONTRATO §17.1): token de kiosco por archivo → cookie de kiosco, solo desde el equipo."""
from __future__ import annotations

import httpx
import pytest

from tests.api.conftest import Harness, api  # noqa: F401  (fixture del arnés de la API)
from vms.core.settings import VmsSettings

TOKEN = "token-kiosco-de-pruebas"  # el de tests/conftest.py


async def test_kiosk_session_sets_signed_cookie_and_opens_wall(api: Harness) -> None:  # noqa: F811
    c = api.client()
    r = await c.post("/api/local/kiosk-session", json={"token": TOKEN, "next": "/wall/3"})
    assert r.status_code == 204, r.text
    cookie = r.headers["set-cookie"]
    assert "vms_session=k1." in cookie and "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    assert "max-age" not in cookie.lower(), "cookie de sesión del WebView (sin Max-Age), como el kiosco de la v1"
    assert r.headers["cache-control"] == "no-store"
    me = (await c.get("/api/auth/me")).json()
    assert me == {"username": "kiosco", "role": "kiosk", "kiosk": True}
    assert (await c.get("/api/walls/3")).status_code == 200
    # solo lectura: el kiosco no administra nada
    assert (await c.get("/api/users")).status_code == 403


async def test_kiosk_session_rejects_bad_token_without_cookie_and_rate_limits(api: Harness) -> None:  # noqa: F811
    c = api.client()
    r = await c.post("/api/local/kiosk-session", json={"token": "otro-token", "next": "/wall/1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_kiosk_token"
    assert "set-cookie" not in r.headers
    for _ in range(12):
        r = await c.post("/api/local/kiosk-session", json={"token": "otro-token", "next": "/wall/1"})
    assert r.status_code == 429 and r.json()["error"]["details"]["retry_after"] > 0
    # con el límite superado, ni siquiera el token correcto entra (no hay oráculo)
    r = await c.post("/api/local/kiosk-session", json={"token": TOKEN, "next": "/wall/1"})
    assert r.status_code == 429


async def test_kiosk_session_only_from_this_computer(api: Harness) -> None:  # noqa: F811
    api.state.settings = api.state.settings.model_copy(update={"kiosk_allow_remote": True})
    remote = api.client(ip="192.168.1.50")
    r = await remote.post("/api/local/kiosk-session", json={"token": TOKEN, "next": "/wall/1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "kiosk_remote"
    assert (await remote.get("/api/local/kiosk")).status_code == 403
    v6 = api.client(ip="::1")
    assert (await v6.post("/api/local/kiosk-session", json={"token": TOKEN, "next": "/wall/1"})).status_code == 204


@pytest.mark.parametrize("nxt", ["//evil.example/x", "/", "/wall/5", "/wall/1?x=1", "https://evil.example/", "/status"])
async def test_kiosk_session_next_is_a_wall(api: Harness, nxt: str) -> None:  # noqa: F811
    r = await api.client().post("/api/local/kiosk-session", json={"token": TOKEN, "next": nxt})
    assert r.status_code == 422


async def test_kiosk_session_needs_csrf_header_and_kiosk_enabled(api: Harness, settings: VmsSettings) -> None:  # noqa: F811
    no_csrf = api.client(csrf=False)
    r = await no_csrf.post("/api/local/kiosk-session", json={"token": TOKEN, "next": "/wall/1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    api.state.settings = api.state.settings.model_copy(update={"kiosk_token": None})
    r = await api.client().post("/api/local/kiosk-session", json={"token": TOKEN, "next": "/wall/1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "kiosk_disabled"


async def test_exchange_page_has_no_scripts_and_strict_csp(api: Harness) -> None:  # noqa: F811
    r = await api.client().get("/api/local/kiosk")
    assert r.status_code == 200
    assert "<script" not in r.text.lower()
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "script-src" not in csp
    assert r.headers["cache-control"] == "no-store"
    err = await api.client().get("/api/local/kiosk", params={"error": "401<script>"})
    assert "<script>" not in err.text and "No se pudo abrir el muro" in err.text


async def test_kiosk_cookie_from_the_viewer_survives_backend_restart(api: Harness) -> None:  # noqa: F811
    """Backend reiniciado → el muro sigue sin login: la cookie es firmada (no vive en memoria)."""
    c = api.client()
    assert (await c.post("/api/local/kiosk-session", json={"token": TOKEN, "next": "/wall/1"})).status_code == 204
    cookie = c.cookies.get("vms_session")
    assert cookie and cookie.startswith("k1.")
    from vms.api import create_app
    from tests.fakes import FakeEngine
    app2 = create_app(api.settings, engine=FakeEngine(), credential_store=api.creds, heartbeat=False, start_engine=False)
    transport = httpx.ASGITransport(app=app2, client=("127.0.0.1", 50001))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                 cookies={"vms_session": cookie}, headers={"X-Requested-With": "vms"}) as c2:
        r = await c2.get("/api/auth/me")
        assert r.status_code == 200 and r.json()["kiosk"] is True
