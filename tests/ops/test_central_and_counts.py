"""Vista central «tiendas con problemas hoy», CSV de problemas y CSV de conteos (CONTRATO §18.15-§18.16;
criterios 2 y 11 de B6). Con PostgreSQL de pruebas (pgserver) y los datos sembrados del panel central."""
from __future__ import annotations

import csv
import io
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from tests.api.conftest import ADMIN_PW as STORE_ADMIN_PW
from tests.central.conftest import (admin_client, central_app, central_settings, client_factory,  # noqa: F401
                                    seeded_dsn, utc)
from tests.fakes import FakeEngine
from vms.ops.counts import CountRow, parse_range, render_csv

pytestmark = pytest.mark.needs_postgres

HEALTH_BAD = {"report_date": "2026-10-07", "status": "critical", "score_min": 0, "cameras_critical": 1,
              "cameras_warning": 1, "clock_worst_s": 45.0, "forecast_days": 12.5,
              "problems": ["Cajas: cámara tapada (puntuación 0)", "Puerta: 12 min sin grabar"]}
HEALTH_OK = {"report_date": "2026-10-07", "status": "ok", "score_min": 96, "cameras_critical": 0,
             "cameras_warning": 0, "clock_worst_s": 0.4, "forecast_days": 41.0, "problems": []}


def _set_health(dsn: str, site: str, health: dict[str, Any] | None, last_seen: str = "2026-10-07T10:29:30Z") -> None:
    payload: dict[str, Any] = {"interval_s": 60}
    if health is not None:
        payload["health"] = health
        payload["evidence_key"] = {"key_id": "ab" * 32, "public_key": "AAAA"}
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("""INSERT INTO site_heartbeats (site_id, last_seen, hostname, version, status, payload)
                     VALUES (%s, %s, 'PC', '2.0.0', 'ok', %s)
                     ON CONFLICT (site_id) DO UPDATE SET payload = EXCLUDED.payload, last_seen = EXCLUDED.last_seen""",
                  (site, utc(last_seen), Jsonb(payload)))


async def test_problems_sorted_by_severity_and_csv(admin_client: httpx.AsyncClient, seeded_dsn: str) -> None:  # noqa: F811
    _set_health(seeded_dsn, "site-bcn-001", HEALTH_BAD)
    _set_health(seeded_dsn, "site-mad-002", HEALTH_OK, last_seen="2026-10-07T10:29:00Z")
    r = await admin_client.get("/api/ops/problems")
    assert r.status_code == 200, r.text
    sites = r.json()["sites"]
    order = [s["site_id"] for s in sites]
    assert order[0] == "site-bcn-001" and order[-1] == "site-mad-002", order
    can = next(s for s in sites if s["site_id"] == "site-can-003")
    assert can["status"] == "nodata" and "nunca" in can["problems"][0]
    assert sites[0]["problems"] == HEALTH_BAD["problems"] and sites[0]["cameras_critical"] == 1
    assert r.json()["summary"] == {"critical": 1, "nodata": 1, "warning": 0, "ok": 1}
    # otro día: sin datos de ese día (no se inventa)
    other = (await admin_client.get("/api/ops/problems", params={"date": "2026-10-01"})).json()["sites"]
    assert all(s["status"] == "nodata" for s in other)
    # CSV para el parte de mantenimiento
    r = await admin_client.get("/api/ops/problems.csv")
    assert r.content.startswith(b"\xef\xbb\xbf") and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][0] == "tienda" and rows[1][2] == "grave" and "cámara tapada" in rows[1][-1]
    # detalle de una sede, con la clave pública de evidencias
    d = (await admin_client.get("/api/ops/sites/site-bcn-001/health")).json()
    assert d["health"]["score_min"] == 0 and d["evidence_key"]["key_id"] == "ab" * 32
    assert (await admin_client.get("/api/ops/sites/no-existe-9/health")).status_code == 404
    page = await admin_client.get("/ops")
    assert page.status_code == 200 and "Tiendas con problemas" in page.text


async def test_stale_heartbeat_is_a_problem(admin_client: httpx.AsyncClient, seeded_dsn: str) -> None:  # noqa: F811
    _set_health(seeded_dsn, "site-mad-002", HEALTH_OK, last_seen="2026-10-07T09:00:00Z")   # NOW = 10:30
    s = next(x for x in (await admin_client.get("/api/ops/problems")).json()["sites"] if x["site_id"] == "site-mad-002")
    assert s["status"] == "critical" and s["problems"][0].startswith("Sin latido desde hace 90 min")


async def test_central_counts_csv(admin_client: httpx.AsyncClient) -> None:  # noqa: F811
    r = await admin_client.get("/api/sites/site-bcn-001/counts.csv",
                               params={"from": "2026-10-07", "to": "2026-10-07", "bucket": "hour"})
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert rows[0] == ["tienda", "fecha", "hora", "entradas", "salidas", "cola_media", "cola_max"]
    by_hour = {r[2]: r for r in rows[1:]}
    # 22:00 UTC del 6 = 00:00 del 7 en Madrid; 08:15 UTC = 10:15; cola 08:00-08:01 UTC = 10:00 local
    assert by_hour["00:00"][3:5] == ["3", "0"] and by_hour["00:00"][1] == "07/10/2026"
    assert by_hour["10:00"][3:5] == ["10", "4"] and by_hour["10:00"][5] == "4,0" and by_hour["10:00"][6] == "6"
    assert rows[1][0] == "Tienda Gràcia (BCN1)"
    r = await admin_client.get("/api/counts.csv", params={"sites": "site-bcn-001,site-mad-002", "from": "2026-10-07",
                                                         "to": "2026-10-07", "bucket": "day"})
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 3 and {r[0] for r in rows[1:]} == {"Tienda Gràcia (BCN1)", "Tienda Chamberí (MAD2)"}
    assert all(r[2] == "" for r in rows[1:]), "por día no lleva hora"
    assert (await admin_client.get("/api/counts.csv", params={"sites": "x"})).status_code == 422


async def test_store_counts_csv_needs_pg_and_uses_site_timezone(settings: Any, credential_store: Any,
                                                                 seeded_dsn: str) -> None:  # noqa: F811
    from vms.api import create_app

    s = settings.model_copy(update={"admin_initial_password": SecretStr(STORE_ADMIN_PW), "site_id": "site-bcn-001"})
    app = create_app(s, engine=FakeEngine(), credential_store=credential_store, heartbeat=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"X-Requested-With": "vms"}) as c:
            await c.post("/api/auth/login", json={"username": "admin", "password": STORE_ADMIN_PW})
            r = await c.get("/api/analytics/counts.csv")
            assert r.status_code == 503 and r.json()["error"]["code"] == "not_configured"
    s2 = s.model_copy(update={"pg_dsn": SecretStr(seeded_dsn)})
    app = create_app(s2, engine=FakeEngine(), credential_store=credential_store, heartbeat=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"X-Requested-With": "vms"}) as c:
            await c.post("/api/auth/login", json={"username": "admin", "password": STORE_ADMIN_PW})
            r = await c.get("/api/analytics/counts.csv", params={"from": "2026-10-07", "to": "2026-10-07"})
            assert r.status_code == 200, r.text
            assert 'filename="conteos_site-bcn-001_2026-10-07_2026-10-07.csv"' in r.headers["content-disposition"]
            rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
            assert ["00:00", "3", "0"] == rows[1][2:5]
            r = await c.get("/api/analytics/counts.csv", params={"from": "2026-01-01", "to": "2026-12-31",
                                                                 "bucket": "hour"})
            assert r.status_code == 422, "por horas, como mucho 62 días"


def test_csv_format_excel_spanish() -> None:
    from datetime import datetime
    rows = [CountRow("Tienda 37", datetime(2026, 10, 7, 9, 0), 12, 3, 2.25, 5),
            CountRow("Tienda 37", datetime(2026, 10, 7, 10, 0), 0, 0, None, None)]
    out = render_csv(rows, "hour").decode("utf-8")
    assert out.startswith("﻿") and "\r\n" in out
    assert "Tienda 37;07/10/2026;09:00;12;3;2,2;5" in out or "Tienda 37;07/10/2026;09:00;12;3;2,3;5" in out
    assert "Tienda 37;07/10/2026;10:00;0;0;;" in out
    start, end = parse_range("2026-03-29", "2026-03-29", "Europe/Madrid", "hour")
    assert (end - start).total_seconds() == 23 * 3600, "el día del cambio de hora tiene 23 h"
