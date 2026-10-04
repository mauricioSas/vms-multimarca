"""Latido: escritura directa (HeartbeatSender, CONTRATO §7.2) y agente HTTP de sede."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from central.agent import AgentConfigError, HeartbeatAgent, read_analytics_status
from central.heartbeat import HeartbeatPayload, HeartbeatSender, SiteInfo, record_heartbeat
from central.settings import AgentSettings
from tests.central.conftest import CSRF
from vms.core.models import Site
from vms.core.paths import AppPaths

PAYLOAD = {"version": "0.1.0", "hostname": "TIENDA-001", "uptime_s": 86400, "status": "ok",
           "engine": {"running": True, "restarts": 0},
           "cameras": [{"camera_id": "cam-1a2b3c4d", "name": "Puerta", "online": True, "recording": True}],
           "cameras_total": 1, "cameras_online": 1, "disk": {"percent": 63.2, "free_gb": 812.4},
           "analytics": {"running": True, "stale": False}}


# =========================================================================== escritura directa
@pytest.mark.needs_postgres
async def test_record_heartbeat_upserts_and_keeps_name(pg_dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
        await record_heartbeat(conn, SiteInfo(id="site-bcn-001", name="Gràcia", code="B1", timezone="Europe/Madrid"),
                               HeartbeatPayload.model_validate(PAYLOAD))
        # segundo latido sin nombre y con zona horaria inventada: no pisa lo guardado
        p2 = {**PAYLOAD, "status": "raro", "cameras": [{"camera_id": "cam-1a2b3c4d", "name": "Puerta 2"}]}
        await record_heartbeat(conn, SiteInfo(id="site-bcn-001", timezone="Luna/Base"),
                               HeartbeatPayload.model_validate(p2))
        await conn.commit()
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT name, code, timezone FROM sites").fetchall() == [("Gràcia", "B1", "Europe/Madrid")]
        assert c.execute("SELECT name FROM site_cameras").fetchall() == [("Puerta 2",)]
        status, payload = c.execute("SELECT status, payload FROM site_heartbeats").fetchone()  # type: ignore[misc]
        assert status == "degraded"  # estado desconocido → «degraded», nunca un valor fuera del CHECK
        assert payload["disk"] == {"percent": 63.2, "free_gb": 812.4}


@pytest.mark.needs_postgres
async def test_heartbeat_sender_writes_periodically(pg_dsn: str) -> None:
    calls = 0

    async def collect() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {**PAYLOAD, "hostname": ""}

    sender = HeartbeatSender(pg_dsn, Site(id="site-test-01", name="Prueba"), interval_s=1, collect=collect)
    await sender.start()
    for _ in range(50):
        if sender.sent >= 2:
            break
        await asyncio.sleep(0.1)
    await sender.stop()
    assert sender.sent >= 2 and sender.failures == 0 and not sender.running
    with psycopg.connect(pg_dsn) as c:
        row = c.execute("SELECT hostname, payload->>'interval_s' FROM site_heartbeats "
                        "WHERE site_id = 'site-test-01'").fetchone()
    assert row is not None and row[0] != "" and row[1] == "10"  # nombre del equipo y mínimo de 10 s


async def test_heartbeat_sender_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    from tests.conftest import get_free_port

    async def collect() -> dict[str, Any]:
        return PAYLOAD

    dsn = f"postgresql://usuario:clave-secreta@127.0.0.1:{get_free_port()}/vms"
    sender = HeartbeatSender(dsn, Site(id="site-test-01"), interval_s=60, collect=collect, connect_timeout=2)
    assert await sender.send_once() is False
    assert sender.failures == 1 and sender.last_error
    assert "clave-secreta" not in sender.last_error

    async def broken() -> dict[str, Any]:
        raise RuntimeError("el backend explotó")

    sender2 = HeartbeatSender(dsn, Site(id="site-test-01"), interval_s=60, collect=broken)
    await sender2.start()
    await asyncio.sleep(0.2)
    await sender2.stop()
    assert sender2.failures == 1
    assert "clave-secreta" not in caplog.text


async def test_sender_reads_current_site_each_beat(pg_dsn: str) -> None:
    """El backend pasa una función: un cambio de nombre desde el panel llega a la central sin reiniciar."""
    current = {"site": Site(id="site-test-02", name="Sede")}

    async def collect() -> dict[str, Any]:
        return PAYLOAD

    sender = HeartbeatSender(pg_dsn, lambda: current["site"], interval_s=60, collect=collect)
    assert await sender.send_once()
    current["site"] = Site(id="site-test-02", name="Tienda Gràcia", timezone="Europe/Madrid")
    assert await sender.send_once()
    with psycopg.connect(pg_dsn) as c:
        row = c.execute("SELECT name FROM sites WHERE site_id = 'site-test-02'").fetchone()
    assert row is not None and row[0] == "Tienda Gràcia"


# =========================================================================== agente HTTP
def backend_transport(*, status_ok: bool = True, fail_health: bool = False,
                      calls: list[str] | None = None) -> httpx.MockTransport:
    """Imita el backend VMS: /api/health libre, /api/status y /api/settings con sesión."""
    state = {"logged": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{req.method} {req.url.path}")
        path = req.url.path
        if path == "/api/health":
            if fail_health:
                raise httpx.ConnectError("Conexión rechazada")
            return httpx.Response(200, json={"status": "degraded", "version": "0.1.0", "uptime_s": 120.5,
                                             "engine": {"running": True, "api_ok": True}})
        if path == "/api/auth/login":
            assert req.headers.get("x-requested-with") == "vms"
            body = json.loads(req.content)
            if body == {"username": "agente", "password": "agente-123"}:
                state["logged"] = True
                return httpx.Response(200, json={"user": {}}, headers={"set-cookie": "vms_session=abc; Path=/"})
            return httpx.Response(401, json={"error": {"code": "invalid_credentials"}})
        if not state["logged"] or "vms_session=abc" not in req.headers.get("cookie", ""):
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if path == "/api/status" and status_ok:
            return httpx.Response(200, json={
                "engine": {"running": True, "restarts": 2, "api_ok": True},
                "disk": {"path": "/rec", "total": 1000, "used": 700, "free": 300_000_000_000, "percent": 70.04},
                "cameras": [{"camera_id": "cam-door0001", "name": "Puerta", "online": True, "recording": True},
                            {"camera_id": "cam-cash0001", "name": "Cajas", "online": False, "recording": False}],
                "analytics": {"running": True, "stale": False}})
        if path == "/api/settings":
            return httpx.Response(200, json={"site": {"id": "site-bcn-001", "name": "Tienda Gràcia", "code": "BCN1",
                                                      "timezone": "Europe/Madrid"}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    return httpx.MockTransport(handler)


def agent_settings(tmp_path: Path, **kw: Any) -> AgentSettings:
    base = {"data_dir": tmp_path / "datos", "site_id": "site-bcn-001", "central_url": "http://central.test",
            "site_token": "vms_token", "username": "agente", "password": "agente-123", "interval_seconds": 60}
    base.update(kw)
    return AgentSettings(_env_file=None, **base)  # type: ignore[call-arg]


def test_agent_requires_configuration(tmp_path: Path) -> None:
    with pytest.raises(AgentConfigError, match="VMS_CENTRAL_URL"):
        HeartbeatAgent(agent_settings(tmp_path, central_url=None))
    with pytest.raises(AgentConfigError, match="VMS_SITE_TOKEN"):
        HeartbeatAgent(agent_settings(tmp_path, site_token=None))


async def test_agent_collects_from_backend(tmp_path: Path) -> None:
    calls: list[str] = []
    agent = HeartbeatAgent(agent_settings(tmp_path), backend_transport=backend_transport(calls=calls))
    try:
        site, payload = await agent.collect()
        # segunda recogida: reutiliza la sesión (no vuelve a hacer login)
        await agent.collect()
    finally:
        await agent.aclose()
    assert site == SiteInfo(id="site-bcn-001", name="Tienda Gràcia", code="BCN1", timezone="Europe/Madrid")
    assert payload.status == "degraded" and payload.version == "0.1.0"
    assert payload.engine == {"running": True, "restarts": 2, "api_ok": True}
    assert payload.cameras_total == 2 and payload.cameras_online == 1
    assert payload.disk == {"percent": 70.0, "free_gb": 300.0}
    assert payload.interval_s == 60 and payload.hostname
    assert calls.count("POST /api/auth/login") == 1


async def test_agent_without_user_and_backend_down(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "datos").ensure()
    (paths.analytics_dir / "status.json").write_text(json.dumps(
        {"running": True, "updated_at": "2000-01-01T00:00:00Z"}), encoding="utf-8")
    agent = HeartbeatAgent(agent_settings(tmp_path, username=None, password=None),
                           backend_transport=backend_transport(fail_health=True))
    try:
        site, payload = await agent.collect()
    finally:
        await agent.aclose()
    assert site.name == "" and payload.status == "down"
    assert payload.engine == {"running": False}
    assert payload.model_extra and payload.model_extra["backend_reachable"] is False
    assert payload.analytics == {"running": False, "stale": True}   # status.json viejo
    assert payload.disk and 0 <= payload.disk["percent"] <= 100     # disco local


def test_read_analytics_status_handles_garbage(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path).ensure()
    assert read_analytics_status(paths) is None
    (paths.analytics_dir / "status.json").write_text("{roto", encoding="utf-8")
    assert read_analytics_status(paths) == {"running": False, "stale": True}


async def test_agent_retries_then_gives_up_without_accumulating(tmp_path: Path) -> None:
    seen: list[int] = []

    def central(req: httpx.Request) -> httpx.Response:
        seen.append(1)
        assert req.headers["authorization"] == "Bearer vms_token"
        return httpx.Response(503, json={"error": {"code": "db_unavailable"}})

    agent = HeartbeatAgent(agent_settings(tmp_path), backend_transport=backend_transport(),
                           central_transport=httpx.MockTransport(central), retry_delays=(0.01, 0.01))
    try:
        assert await agent.run_once() is False
        assert len(seen) == 3 and agent.failures == 1
        # rechazo por token: no se reintenta
        seen.clear()

        def deny(req: httpx.Request) -> httpx.Response:
            seen.append(1)
            return httpx.Response(401, json={"error": {"code": "invalid_token"}})

        agent._central._transport = httpx.MockTransport(deny)  # type: ignore[attr-defined]
        assert await agent.run_once() is False
        assert len(seen) == 1
    finally:
        await agent.aclose()


@pytest.mark.needs_postgres
async def test_agent_end_to_end_with_central(tmp_path: Path, central_app: Any, admin_client: httpx.AsyncClient,
                                             seeded_dsn: str) -> None:
    """Agente real → panel central real (ASGI) → PostgreSQL real; primero falla la red y luego entra."""
    token = (await admin_client.post("/api/site-tokens/site-bcn-001", headers=CSRF)).json()["token"]
    asgi = httpx.ASGITransport(app=central_app, client=("100.64.0.7", 1))
    attempts = {"n": 0}

    class FlakyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectTimeout("La VPN aún no está arriba")
            return await asgi.handle_async_request(request)

    agent = HeartbeatAgent(agent_settings(tmp_path, site_token=token), backend_transport=backend_transport(),
                           central_transport=FlakyTransport(), retry_delays=(0.01,))
    try:
        assert await agent.run_once() is True
    finally:
        await agent.aclose()
    assert attempts["n"] == 2
    with psycopg.connect(seeded_dsn) as c:
        hb = c.execute("SELECT status, payload->>'cameras_online', payload->'engine'->>'restarts' "
                       "FROM site_heartbeats WHERE site_id = 'site-bcn-001'").fetchone()
    assert hb == ("degraded", "1", "2")
    sites = (await admin_client.get("/api/sites")).json()["sites"]
    bcn = next(s for s in sites if s["site_id"] == "site-bcn-001")
    assert bcn["cameras_total"] == 2 and bcn["disk_percent"] == 70.0


def test_agent_cli_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture[str]) -> None:
    """`python -m central.agent --dry-run` funciona sin central ni backend (backend caído → status down)."""
    from central.agent import main
    from tests.conftest import get_free_port

    monkeypatch.setenv("VMS_DATA_DIR", str(tmp_path / "datos"))
    monkeypatch.setenv("VMS_SITE_ID", "site-cli-001")
    monkeypatch.setenv("VMS_AGENT_BACKEND_URL", f"http://127.0.0.1:{get_free_port()}")
    monkeypatch.setenv("VMS_AGENT_TIMEOUT_SECONDS", "2")
    assert main(["--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["site"]["id"] == "site-cli-001" and out["payload"]["status"] == "down"


def test_agent_cli_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from central.agent import main

    monkeypatch.setenv("VMS_DATA_DIR", str(tmp_path / "datos"))
    assert main(["--once"]) == 2


__all__: list[Callable[..., Any]] = []
