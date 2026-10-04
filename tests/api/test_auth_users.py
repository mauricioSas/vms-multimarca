"""Sesiones, CSRF, límite de intentos, primer arranque, kiosco y gestión de usuarios (CONTRATO §6.1-6.2)."""
from __future__ import annotations

import pytest

from tests.api.conftest import ADMIN_PW, Harness


async def test_login_sets_secure_cookie_and_me(api: Harness) -> None:
    c = api.client()
    r = await c.post("/api/auth/login", json={"username": "ADMIN", "password": ADMIN_PW})  # insensible a mayúsculas
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "admin" and "password_hash" not in r.text
    cookie = r.headers["set-cookie"].lower()
    assert "vms_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    me = (await c.get("/api/auth/me")).json()
    assert me == {"username": "admin", "role": "admin", "kiosk": False}
    assert (await c.post("/api/auth/logout")).status_code == 204
    r = await c.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


async def test_bad_login_and_rate_limit(api: Harness) -> None:
    c = api.client(ip="10.9.9.9")
    for _ in range(5):
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "mala-mala"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert r.json()["error"]["code"] == "rate_limited"
    # otra IP no queda bloqueada
    await api.login(ip="10.9.9.10")
    # un usuario inexistente responde igual que una contraseña mala
    r = await api.client().post("/api/auth/login", json={"username": "nadie", "password": "xxxxxxxx"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


async def test_csrf_header_required_for_modifying_requests(api: Harness) -> None:
    c = api.client(csrf=False)
    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    admin = await api.login()
    r = await admin.post("/api/users", json={"username": "x1x", "password": "12345678"},
                         headers={"X-Requested-With": "otra"})
    assert r.status_code == 403
    # GET no lo necesita y no hay CORS
    r = await admin.get("/api/users", headers={"Origin": "http://malo.example", "X-Requested-With": ""})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_unauthenticated_and_validation_errors_use_contract_format(api: Harness) -> None:
    c = api.client()
    r = await c.get("/api/devices")
    assert r.status_code == 401 and set(r.json()["error"]) == {"code", "message", "details"}
    admin = await api.login()
    r = await admin.post("/api/devices", json={"name": "", "vendor": "sony", "host": "rtsp://x"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    locs = {tuple(f["loc"]) for f in err["details"]["fields"]}
    assert ("name",) in locs and ("vendor",) in locs and ("host",) in locs
    msgs = " ".join(f["msg"] for f in err["details"]["fields"])
    assert "sin rtsp://" in msgs  # mensaje del validador en español
    r = await admin.get("/api/no-existe")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_setup_only_without_users_and_from_localhost(api: Harness) -> None:
    c = api.client()
    assert (await c.get("/api/auth/setup")).json() == {"needed": False}
    r = await c.post("/api/auth/setup", json={"username": "otro", "password": "12345678"})
    assert r.status_code == 409
    # sistema sin usuarios
    for u in list(api.state.users.all()):
        await api.state.users.delete_user(u.username)
    assert (await c.get("/api/auth/setup")).json() == {"needed": True}
    remote = api.client(ip="192.168.1.50")
    r = await remote.post("/api/auth/setup", json={"username": "jefe", "password": "12345678"})
    assert r.status_code == 403
    r = await c.post("/api/auth/setup", json={"username": "jefe", "password": "corta"})
    assert r.status_code == 422
    r = await c.post("/api/auth/setup", json={"username": "jefe", "password": "12345678"})
    assert r.status_code == 201 and r.json()["user"]["role"] == "admin"
    assert (await c.get("/api/auth/me")).json()["role"] == "admin"  # queda con sesión


async def test_kiosk_session_is_read_only(api: Harness) -> None:
    k = api.client()
    r = await k.get("/api/auth/kiosk", params={"token": "token-kiosco-de-pruebas", "next": "//evil.example/x"})
    assert r.status_code == 303 and r.headers["location"] == "/wall/1"  # sin redirección abierta
    me = (await k.get("/api/auth/me")).json()
    assert me == {"username": "kiosco", "role": "kiosk", "kiosk": True}
    assert (await k.get("/api/walls")).status_code == 200
    assert (await k.get("/api/cameras")).status_code == 200
    assert (await k.get("/api/devices")).status_code == 403
    assert (await k.put("/api/walls/1", json={"grid": 9})).status_code == 403
    assert (await k.get("/api/status")).status_code == 403

    bad = api.client()
    r = await bad.get("/api/auth/kiosk", params={"token": "otro"})
    assert r.status_code == 401
    remote = api.client(ip="192.168.1.20")
    r = await remote.get("/api/auth/kiosk", params={"token": "token-kiosco-de-pruebas"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "kiosk_remote"


async def test_users_crud_and_admin_protections(api: Harness) -> None:
    admin = await api.login()
    r = await admin.post("/api/users", json={"username": "ana.lopez", "password": "Segura#123", "role": "operator"})
    assert r.status_code == 201 and r.json()["role"] == "operator" and "password" not in r.text
    assert (await admin.post("/api/users", json={"username": "ANA.LOPEZ", "password": "Segura#123"})).status_code == 409
    users = (await admin.get("/api/users")).json()
    assert [u["username"] for u in users] == ["admin", "ana.lopez"]

    op = await api.login("ana.lopez", "Segura#123")
    assert (await op.get("/api/users")).status_code == 403
    assert (await op.get("/api/cameras")).status_code == 200

    # Deshabilitar cierra sus sesiones
    r = await admin.patch("/api/users/ana.lopez", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert (await op.get("/api/cameras")).status_code == 401

    # El último administrador no se puede degradar, deshabilitar ni borrar; uno no se borra a sí mismo
    assert (await admin.patch("/api/users/admin", json={"role": "operator"})).status_code == 409
    assert (await admin.patch("/api/users/admin", json={"enabled": False})).status_code == 409
    assert (await admin.delete("/api/users/admin")).status_code == 409
    await admin.post("/api/users", json={"username": "admin2", "password": "Segura#123", "role": "admin"})
    admin2 = await api.login("admin2", "Segura#123")
    assert (await admin.delete("/api/users/admin")).status_code == 409  # a sí mismo
    assert (await admin2.delete("/api/users/admin")).status_code == 204
    assert (await admin.get("/api/users")).status_code == 401  # su sesión ya no vale
    assert (await admin2.delete("/api/users/nadie")).status_code == 404

    # Cambio de contraseña propia: la sesión actual sigue
    r = await admin2.patch("/api/users/admin2", json={"password": "Nueva#12345"})
    assert r.status_code == 200 and (await admin2.get("/api/users")).status_code == 200
    await api.login("admin2", "Nueva#12345")


@pytest.mark.parametrize("path", ["/", "/playback", "/analytics", "/wall/3"])
async def test_pages_redirect_to_login_without_session(api: Harness, path: str) -> None:
    r = await api.client().get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


async def test_pages_with_session(api: Harness) -> None:
    admin = await api.login()
    r = await admin.get("/wall/2")
    assert r.status_code in (200, 503) and r.headers["content-type"].startswith("text/html")
    assert (await admin.get("/wall/7")).status_code == 404
    assert (await admin.get("/setup")).headers.get("location") == "/login"
    k = await api.kiosk()
    assert (await k.get("/")).headers["location"] == "/wall/1"
    assert (await k.get("/wall/4")).status_code in (200, 503)
