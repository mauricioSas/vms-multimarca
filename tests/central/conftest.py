"""Fixtures del panel central: datos sembrados en una base nueva (pgserver) y clientes HTTP."""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from central.settings import CentralSettings

# «Ahora» fijo: miércoles 7-oct-2026 10:30 UTC = 12:30 en Madrid (CEST, UTC+2), 11:30 en Canarias.
NOW = datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc)
ADMIN_PW = "admin-central-123"
CSRF = {"X-Requested-With": "vms"}


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def seed(dsn: str) -> None:
    """Tres sedes (dos en Madrid y una en Canarias) con conteos, ocupación, alertas e informes."""
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("""INSERT INTO sites (site_id, name, code, timezone) VALUES
            ('site-bcn-001', 'Tienda Gràcia', 'BCN1', 'Europe/Madrid'),
            ('site-mad-002', 'Tienda Chamberí', 'MAD2', 'Europe/Madrid'),
            ('site-can-003', 'Tienda Las Palmas', 'LPA3', 'Atlantic/Canary'),
            ('site-old-004', 'Tienda cerrada', '', 'Europe/Madrid')""")
        c.execute("UPDATE sites SET active = false WHERE site_id = 'site-old-004'")
        c.execute("""INSERT INTO site_cameras (site_id, camera_id, name) VALUES
            ('site-bcn-001', 'cam-door0001', 'Puerta'), ('site-bcn-001', 'cam-cash0001', 'Cajas'),
            ('site-mad-002', 'cam-cash0002', 'Cajas Chamberí')""")
        c.execute("""INSERT INTO analytics_rules (site_id, rule_id, camera_id, kind, name, config) VALUES
            ('site-bcn-001', 'rule-door01', 'cam-door0001', 'line', 'Entrada principal', '{}'),
            ('site-bcn-001', 'rule-queue1', 'cam-cash0001', 'zone', 'Cola cajas', '{"alert_threshold": 5}'),
            ('site-mad-002', 'rule-queue2', 'cam-cash0002', 'zone', 'Cola Chamberí', '{}')""")
        lines = [
            ("site-bcn-001", "2026-10-06T21:59:00Z", 5, 0),   # 23:59 del 6 en Madrid → ayer
            ("site-bcn-001", "2026-10-06T22:00:00Z", 3, 0),   # 00:00 del 7 en Madrid → hoy
            ("site-bcn-001", "2026-10-07T08:15:00Z", 10, 4),  # 10:15 hoy
            ("site-bcn-001", "2026-10-07T10:31:00Z", 100, 0),  # futuro respecto a NOW → no cuenta
            ("site-bcn-001", "2026-09-29T09:00:00Z", 9, 1),   # semana anterior, dentro del mismo tramo
            ("site-bcn-001", "2026-09-30T11:00:00Z", 50, 0),  # semana anterior, después del tramo
            ("site-mad-002", "2026-10-07T07:00:00Z", 20, 18),
            ("site-can-003", "2026-10-06T22:30:00Z", 7, 0),   # 23:30 del 6 en Canarias → ayer
            ("site-can-003", "2026-10-07T09:00:00Z", 2, 1),
        ]
        with c.cursor() as cur:
            cur.executemany("""INSERT INTO line_counts_minute (site_id, rule_id, camera_id, minute, count_in, count_out)
                               VALUES (%s, 'rule-door01', 'cam-door0001', %s, %s, %s)""",
                            [(s, utc(m), i, o) for s, m, i, o in lines])
            cur.executemany("""INSERT INTO zone_occupancy_minute (site_id, rule_id, camera_id, minute, samples,
                                   avg_people, max_people, seconds_over_threshold)
                               VALUES (%s, %s, %s, %s, 60, %s, %s, %s)""", [
                ("site-bcn-001", "rule-queue1", "cam-cash0001", utc("2026-10-07T08:00:00Z"), 3.0, 5, 20),
                ("site-bcn-001", "rule-queue1", "cam-cash0001", utc("2026-10-07T08:01:00Z"), 5.0, 6, 60),
            ])
            cur.executemany("""INSERT INTO queue_alerts (alert_id, site_id, rule_id, camera_id, started_at, ended_at,
                                   peak_people, threshold, notified_at, notify_error)
                               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, 5, %s, %s)""", [
                ("site-bcn-001", "rule-queue1", "cam-cash0001", utc("2026-10-07T08:00:00Z"),
                 utc("2026-10-07T08:05:00Z"), 7, utc("2026-10-07T08:00:05Z"), None),
                ("site-bcn-001", "rule-queue1", "cam-cash0001", utc("2026-10-07T10:00:00Z"), None, 9, None,
                 "timeout"),
                ("site-mad-002", "rule-queue2", "cam-cash0002", utc("2026-10-06T12:00:00Z"),
                 utc("2026-10-06T12:10:00Z"), 6, utc("2026-10-06T12:00:03Z"), None),
            ])
        c.execute("""INSERT INTO site_heartbeats (site_id, last_seen, hostname, version, status, payload) VALUES
            ('site-bcn-001', %s, 'TIENDA-001', '0.1.0', 'degraded', %s),
            ('site-mad-002', %s, 'TIENDA-002', '0.1.0', 'ok', %s)""", (
            utc("2026-10-07T10:29:30Z"), Jsonb({"cameras_total": 2, "cameras_online": 1, "interval_s": 60,
                                               "disk": {"percent": 71.5, "free_gb": 120.0},
                                               "cameras": [{"camera_id": "cam-door0001", "online": True,
                                                            "recording": True},
                                                           {"camera_id": "cam-cash0001", "online": False,
                                                            "recording": False}]}),
            utc("2026-10-07T10:20:00Z"), Jsonb({"interval_s": 60})))
        c.execute("""INSERT INTO weekly_reports (site_id, week_start, status, provider, model, metrics, body_markdown)
                     VALUES ('site-bcn-001', '2026-09-28', 'ok', 'anthropic', 'modelo-x', %s, %s)""",
                  (Jsonb({"entries_total": 1234}), "## Resumen\n\n- Entradas: **1234**\n"))
        c.execute("""INSERT INTO weekly_reports (site_id, week_start, status, provider, metrics, error)
                     VALUES ('site-bcn-001', '2026-09-21', 'error', 'none', '{}', 'Proveedor desactivado')""")


@pytest.fixture
def seeded_dsn(pg_dsn: str) -> str:
    seed(pg_dsn)
    return pg_dsn


@pytest.fixture
def central_settings(tmp_path: Path) -> CentralSettings:
    return CentralSettings(_env_file=None, data_dir=tmp_path / "central",  # type: ignore[call-arg]
                           admin_initial_password=ADMIN_PW, heartbeat_seconds=60, http_port=8700)


@pytest.fixture
async def central_app(central_settings: CentralSettings, seeded_dsn: str) -> AsyncIterator[Any]:
    from central import db
    from central.app import create_app

    pool = db.make_pool(seeded_dsn, 4)
    await pool.open(wait=True, timeout=20)
    app = create_app(central_settings, pool=pool, clock=lambda: NOW)
    async with app.router.lifespan_context(app):
        yield app
    await pool.close()


@pytest.fixture
async def client_factory(central_app: Any) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    clients: list[httpx.AsyncClient] = []

    def make(ip: str = "127.0.0.1") -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=central_app, client=(ip, 40000)),
                              base_url="http://central.test")
        clients.append(c)
        return c

    yield make
    for c in clients:
        await c.aclose()


async def login(client: httpx.AsyncClient, username: str = "admin", password: str = ADMIN_PW) -> None:
    r = await client.post("/api/auth/login", json={"username": username, "password": password}, headers=CSRF)
    assert r.status_code == 200, r.text


@pytest.fixture
async def admin_client(client_factory: Callable[..., httpx.AsyncClient]) -> httpx.AsyncClient:
    c = client_factory()
    await login(c)
    return c


__all__ = ["NOW", "ADMIN_PW", "CSRF", "utc", "seed", "login", "date"]
