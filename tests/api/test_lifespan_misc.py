"""Arranque del backend: motor que no arranca (reintento), latido a PostgreSQL, kiosco deslizante y
detalles de la configuración interna de la analítica."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from tests.api.conftest import ADMIN_PW, HEADERS, Harness, new_device
from tests.fakes import FakeDeviceClient, FakeEngine
from vms.api import app as app_module
from vms.api import create_app
from vms.core.credentials import CredentialStore
from vms.core.errors import EngineUnavailable
from vms.core.settings import VmsSettings


class FlakyEngine(FakeEngine):
    """Falla al arrancar las primeras veces (p. ej. puerto ocupado) y luego funciona."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_calls <= self.failures:
            raise EngineUnavailable("El puerto de la API de MediaMTX ya está ocupado")
        await super().start()


async def test_backend_starts_even_if_engine_fails_and_retries(settings: VmsSettings,
                                                               credential_store: CredentialStore,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    monkeypatch.setattr(app_module, "ENGINE_RETRY_S", 0.2)
    engine = FlakyEngine(failures=2)
    app = create_app(settings.model_copy(update={"admin_initial_password": SecretStr(ADMIN_PW)}), engine=engine,
                     credential_store=credential_store, heartbeat=False, apply_delay=0.01,
                     client_factory=lambda d, p: FakeDeviceClient(d.vendor))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=HEADERS) as c:
            assert (await c.get("/api/health")).json()["status"] == "down"
            await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
            dev = await new_device(c, import_channels=[1])  # la configuración se puede editar igual
            deadline = time.monotonic() + 5
            # start() y la primera aplicación de la configuración son dos pasos: hay que esperar a ambos
            while time.monotonic() < deadline and not (engine.running and dev["cameras"][0] in engine.sources):
                await asyncio.sleep(0.05)
            assert engine.running and engine.start_calls == 3
            assert dev["cameras"][0] in engine.sources  # al arrancar se aplicó la configuración
            assert (await c.get("/api/health")).json()["status"] == "ok"
    assert engine.running is False  # se detuvo al parar el backend


@pytest.mark.needs_postgres
async def test_heartbeat_written_to_postgres(settings: VmsSettings, credential_store: CredentialStore,
                                             pg_dsn: str) -> None:
    s = settings.model_copy(update={"pg_dsn": SecretStr(pg_dsn), "heartbeat_seconds": 10})
    app = create_app(s, engine=FakeEngine(), credential_store=credential_store, apply_delay=0.01)
    row: Any = None
    async with app.router.lifespan_context(app):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
                cur = await conn.execute("SELECT site_id, status, payload FROM site_heartbeats")
                row = await cur.fetchone()
            if row:
                break
            await asyncio.sleep(0.3)
    assert row is not None, "el backend debe escribir el latido si hay VMS_PG_DSN"
    site_id, status, payload = row
    assert site_id == "site-test" and status == "ok"
    assert payload["engine"]["running"] is True and payload["cameras_total"] == 0
    assert payload["disk"]["percent"] == 40.0 and "hostname" in payload


async def test_kiosk_session_slides_and_cookie_lives_with_browser(api: Harness) -> None:
    k = api.client()
    r = await k.get("/api/auth/kiosk", params={"token": "token-kiosco-de-pruebas"})
    assert "max-age" not in r.headers["set-cookie"].lower()
    session = next(iter(api.state.sessions._sessions.values()))
    session.expires = time.time() + 5  # a punto de caducar
    assert (await k.get("/api/walls")).status_code == 200
    assert session.expires > time.time() + 3600  # renovada por el uso


async def test_internal_config_skips_disabled_devices(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1])
    cam = dev["cameras"][0]
    await admin.post("/api/analytics/rules", json={"kind": "line", "camera_id": cam, "name": "Puerta",
                                                    "start": [0, 0.5], "end": [1, 0.5]})
    await admin.put(f"/api/analytics/cameras/{cam}", json={"enabled": True, "fps": 10})
    hdr = {"X-VMS-Internal-Token": "token-interno-de-pruebas"}
    assert len((await admin.get("/api/internal/analytics/config", headers=hdr)).json()["cameras"]) == 1
    await admin.patch(f"/api/devices/{dev['id']}", json={"enabled": False})
    assert (await admin.get("/api/internal/analytics/config", headers=hdr)).json()["cameras"] == []


async def test_snapshot_falls_back_and_reports_device_error(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1])
    api.behavior["reachable"] = False  # el equipo no da snapshot y el RTSP local (falso) no existe
    r = await admin.get(f"/api/cameras/{dev['cameras'][0]}/snapshot")
    assert r.status_code == 502 and r.json()["error"]["code"] == "device_unreachable"
