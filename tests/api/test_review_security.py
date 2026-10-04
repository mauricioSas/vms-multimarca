"""Regresiones de la revisión de seguridad y robustez del backend VMS (API).

- Sesión de kiosco firmada: sobrevive a un reinicio del backend.
- Login: argon2 fuera del bucle, límite por IP, memoria acotada.
- Equipos: cambiar la IP sin volver a escribir la contraseña no la envía a otro sitio.
- Registro de accesos a grabaciones, CSP, aviso RGPD de retención, clear_below.
- El proxy WHEP/reproducción se identifica ante MediaMTX.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable

import httpx
import pytest
from pydantic import SecretStr

from tests.api.conftest import ADMIN_PW, HEADERS, Harness, new_device
from tests.api.test_live_recordings import FakeMediaMtx, setup  # noqa: F401 - fixture reutilizada
from tests.fakes import FakeEngine
from vms.api import create_app
from vms.api import security as security_mod
from vms.api.security import COOKIE_NAME, KioskSigner, LoginLimiter
from vms.core.credentials import CredentialStore
from vms.core.settings import VmsSettings


# --------------------------------------------------------------------------- kiosco tras un reinicio
async def test_kiosk_cookie_survives_backend_restart(api: Harness, settings: VmsSettings,
                                                     credential_store: CredentialStore) -> None:
    k = await api.kiosk()
    cookie = k.cookies.get(COOKIE_NAME)
    assert cookie and cookie.startswith("k1.")
    # «Reinicio»: otra instancia de la app con los mismos ajustes y sesiones vacías en memoria
    app2 = create_app(api.settings, engine=FakeEngine(), credential_store=credential_store, heartbeat=False,
                      apply_delay=0.01)
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2, client=("127.0.0.1", 50000)),
                                     base_url="http://testserver", headers=HEADERS) as c2:
            c2.cookies.set(COOKIE_NAME, cookie)
            r = await c2.get("/api/auth/me")
            assert r.status_code == 200 and r.json() == {"username": "kiosco", "role": "kiosk", "kiosk": True}
            assert (await c2.get("/api/walls/1")).status_code == 200
            assert (await c2.get("/api/devices")).status_code == 403    # sigue siendo solo lectura
            # Cookie manipulada o de otro token de kiosco: no vale
            c2.cookies.set(COOKIE_NAME, cookie[:-2] + ("00" if not cookie.endswith("00") else "11"))
            assert (await c2.get("/api/auth/me")).status_code == 401
            other = KioskSigner("otro-token").issue()
            c2.cookies.set(COOKIE_NAME, other)
            assert (await c2.get("/api/auth/me")).status_code == 401
            # Tras cerrar sesión, la cookie no se puede «recuperar»
            c2.cookies.set(COOKIE_NAME, cookie)
            assert (await c2.post("/api/auth/logout")).status_code == 204
            c2.cookies.set(COOKIE_NAME, cookie)
            assert (await c2.get("/api/auth/me")).status_code == 401


def test_kiosk_signer_expiry_and_user_sessions_unaffected() -> None:
    s = KioskSigner("tok", max_age_s=3600)
    t = s.issue(now=1_000_000)
    assert s.verify(t, now=1_000_000 + 10) == 1_000_000
    assert s.verify(t, now=1_000_000 + 3601) is None
    assert s.verify("abc", now=1_000_000) is None
    store = security_mod.SessionStore(12, kiosk_signer=s)
    user = store.create("admin")
    assert not user.token.startswith("k1.")
    store._sessions.clear()                      # reinicio: las sesiones de usuario sí se pierden
    assert store.get(user.token) is None


# --------------------------------------------------------------------------- login
async def test_login_per_ip_limit_with_random_usernames(api: Harness) -> None:
    c = api.client(ip="10.9.9.9")
    codes = []
    for i in range(25):
        r = await c.post("/api/auth/login", json={"username": f"u{i}", "password": "x"})
        codes.append(r.status_code)
    assert codes[:20] == [401] * 20 and set(codes[20:]) == {429}
    # Otra IP no queda bloqueada, y el admin legítimo entra
    assert (await api.login()).cookies.get(COOKIE_NAME)


def test_login_limiter_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    lim = LoginLimiter(5, 300)
    monkeypatch.setattr(LoginLimiter, "MAX_KEYS", 100)
    for _ in range(5):
        lim.failure("5.5.5.5", "admin")
    for i in range(1000):
        lim.failure("1.2.3.4", f"user{i}")
    assert len(lim) <= 101
    assert lim.retry_after("5.5.5.5", "admin") > 0     # el bloqueo no se pierde por el barrido


async def test_password_check_does_not_block_event_loop(api: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_verify(_h: str, _p: str) -> bool:
        time.sleep(0.3)   # simula argon2 en un mini PC lento
        return False

    monkeypatch.setattr(security_mod, "verify_password", slow_verify)
    monkeypatch.setattr(security_mod, "burn_verify", lambda _p: time.sleep(0.3))
    attackers = [api.client(ip=f"10.0.0.{i}") for i in range(4)]
    tasks = [asyncio.create_task(c.post("/api/auth/login", json={"username": "admin", "password": "mala"}))
             for c in attackers]
    await asyncio.sleep(0.05)
    t0 = time.perf_counter()
    r = await api.client().get("/api/health")
    elapsed = time.perf_counter() - t0
    await asyncio.gather(*tasks)
    assert r.status_code == 200 and elapsed < 0.25, f"/api/health tardó {elapsed:.3f} s durante el ataque"


# --------------------------------------------------------------------------- equipos
async def test_changing_device_address_requires_password_again(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=None)
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"host": "127.0.0.1", "rtsp_port": 9554})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"][0]["loc"] == ["password"]
    assert api.state.config().device(dev["id"]).host == "10.0.0.5"
    # Con la contraseña escrita de nuevo sí se cambia
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"host": "10.0.0.6", "password": "Nueva#1"})
    assert r.status_code == 200 and r.json()["host"] == "10.0.0.6"
    assert api.creds.get_device_password(dev["id"]) == "Nueva#1"
    # Cambiar solo el nombre (o enviar la misma IP) no pide la contraseña
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"name": "Otro nombre", "host": "10.0.0.6"})
    assert r.status_code == 200
    api.tester_calls.clear()
    await admin.post(f"/api/devices/{dev['id']}/test")
    assert api.tester_calls == [("10.0.0.6", "Nueva#1")]


# --------------------------------------------------------------------------- registro de accesos
async def test_recording_access_is_audited(setup: tuple[Harness, FakeMediaMtx, str],  # noqa: F811
                                           caplog: pytest.LogCaptureFixture) -> None:
    api, _fake, cam = setup
    op = await api.operator()
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-04T10:00:05Z", "duration": 30,
                                                                  "format": "mp4", "download": "true"})
        assert r.status_code == 200
        k = await api.kiosk()
        await k.post(f"/api/live/{cam}/main/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
    events = [json.loads(rec.getMessage()) for rec in caplog.records if rec.name == "vms.audit"]
    dl = next(e for e in events if e["event"] == "recording_download")
    assert dl["user"] == "operador" and dl["camera_id"] == cam and dl["duration_s"] == 30
    assert dl["start"].startswith("2026-10-04T10:00:05") and dl["ip"] == "127.0.0.1"
    assert any(e["event"] == "live_main" and e["user"] == "kiosco" for e in events)


def test_audit_log_file(tmp_path: Any) -> None:
    from vms.core.audit import audit
    from vms.core.logging_setup import setup_audit_log

    path = setup_audit_log(tmp_path)
    audit("recording_view", user="ana", ip="10.0.0.2", camera_id="cam-x", start="2026-10-04T10:00:00Z")
    for h in logging.getLogger("vms.audit").handlers:
        h.flush()
    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert '"event": "recording_view"' in line and '"user": "ana"' in line


# --------------------------------------------------------------------------- cabeceras y RGPD
async def test_html_pages_send_csp(api: Harness) -> None:
    r = await api.client().get("/login")
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp
    assert "content-security-policy" not in (await api.client().get("/api/health")).headers


async def test_retention_over_a_month_warns(api: Harness) -> None:
    admin = await api.login()
    r = await admin.put("/api/settings/retention", json={"days": 90, "disk_guard_percent": 85})
    assert r.status_code == 200 and "LOPDGDD" in r.json()["warning"] and r.json()["days"] == 90
    r = await admin.put("/api/settings/retention", json={"days": 30, "disk_guard_percent": 85})
    assert r.json() == {"days": 30, "disk_guard_percent": 85}


async def test_zone_rule_clear_below_must_be_lower_than_threshold(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1])
    cam = dev["cameras"][0]
    body = {"kind": "zone", "camera_id": cam, "name": "Cola", "alert_threshold": 5, "clear_below": 8,
            "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]}
    r = await admin.post("/api/analytics/rules", json=body)
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["loc"] == ["clear_below"]
    r = await admin.post("/api/analytics/rules", json={**body, "clear_below": 3})
    assert r.status_code == 201
    rid = r.json()["id"]
    r = await admin.put(f"/api/analytics/rules/{rid}", json={**body, "clear_below": 3, "alert_threshold": 3})
    assert r.status_code == 422   # cambiar solo el umbral también se comprueba


# --------------------------------------------------------------------------- proxy autenticado
class _AuthEngine(FakeEngine):
    def http_credentials(self) -> tuple[str, str]:
        return ("vms-backend", "secreto-de-prueba")


async def test_proxy_authenticates_to_mediamtx(settings: VmsSettings, credential_store: CredentialStore,
                                               mock_server: Callable[[Any], Any]) -> None:
    fake = FakeMediaMtx()
    srv = mock_server(fake.app)
    engine = _AuthEngine()
    s = settings.model_copy(update={"admin_initial_password": SecretStr(ADMIN_PW)})
    app = create_app(s, engine=engine, credential_store=credential_store, heartbeat=False, apply_delay=0.01)
    engine.whep_url = lambda cid, stream: f"{srv.base_url}/{cid}/{stream}/whep"  # type: ignore[method-assign]
    async with app.router.lifespan_context(app):
        h = Harness(app, engine, credential_store, s)
        admin = await h.login()
        r = await admin.post("/api/devices", json={"name": "Cam", "vendor": "generic", "kind": "camera",
                                                   "host": "10.0.0.7", "username": "u", "password": "p"})
        dev = r.json()
        r = await admin.post("/api/cameras", json={"name": "Cam", "device_id": dev["id"], "channel": 1,
                                                   "main_path": "/live", "has_sub": False})
        cam = r.json()["id"]
        await h.settle()
        r = await admin.post(f"/api/live/{cam}/main/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
        assert r.status_code == 201
        sent = fake.calls[-1][3].get("authorization", "")
        assert sent == "Basic " + base64.b64encode(b"vms-backend:secreto-de-prueba").decode()
        await h.close()
