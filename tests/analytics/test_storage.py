"""Persistencia en PostgreSQL (pgserver) con cola en disco y escrituras idempotentes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from analytics.aggregation import line_record, zone_record
from analytics.storage import Persistence, PgStore, Spool
from tests.conftest import get_free_port

pytestmark = [pytest.mark.needs_postgres]

SITE = "site-bcn-001"
M0 = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
M1 = datetime(2026, 10, 5, 10, 1, tzinfo=timezone.utc)


def meta() -> list[dict[str, object]]:
    return [{"type": "site", "name": "Tienda Gràcia", "code": "T001", "timezone": "Europe/Madrid"},
            {"type": "camera", "camera_id": "cam-00000001", "name": "Puerta"},
            {"type": "rule", "rule_id": "rule-00000001", "camera_id": "cam-00000001", "kind": "line",
             "name": "Entrada", "config": {"start": [0.1, 0.5], "end": [0.9, 0.5]}, "active": True},
            {"type": "rule", "rule_id": "rule-00000009", "camera_id": "cam-00000001", "kind": "zone",
             "name": "Vieja", "config": {}, "active": True},
            {"type": "rules_active", "rule_ids": ["rule-00000001"]}]


def q(dsn: str, sql: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as conn:
        return list(conn.execute(sql).fetchall())  # type: ignore[arg-type]


async def test_write_counts_meta_and_idempotent_upserts(pg_dsn: str) -> None:
    store = PgStore(pg_dsn, SITE)
    batch = meta() + [line_record("rule-00000001", "cam-00000001", M0, 5, 3),
                      zone_record("rule-00000002", "cam-00000002", M0, 60, 2.5, 6, 30.4)]
    await store.write(batch)
    await store.write(batch)                     # reintento del mismo lote: no duplica
    await store.write([line_record("rule-00000001", "cam-00000001", M0, 7, 3)])   # corrección del minuto
    await store.close()
    assert q(pg_dsn, "SELECT name, code, timezone FROM sites") == [("Tienda Gràcia", "T001", "Europe/Madrid")]
    assert q(pg_dsn, "SELECT count_in, count_out FROM line_counts_minute") == [(7, 3)]
    assert q(pg_dsn, "SELECT samples, max_people, seconds_over_threshold FROM zone_occupancy_minute") == [(60, 6, 30)]
    assert q(pg_dsn, "SELECT rule_id, active FROM analytics_rules ORDER BY 1") == [
        ("rule-00000001", True), ("rule-00000009", False)]
    assert q(pg_dsn, "SELECT name FROM site_cameras") == [("Puerta",)]


async def test_alert_upsert_semantics(pg_dsn: str) -> None:
    store = PgStore(pg_dsn, SITE, "Tienda")
    aid = "9b2f7f3e-7a59-4b0e-9a8e-1c2d3e4f5a6b"
    base = {"type": "alert", "alert_id": aid, "rule_id": "rule-z", "camera_id": "cam-2",
            "started_at": M0.isoformat(), "ended_at": None, "peak_people": 6, "threshold": 5,
            "notified_at": None, "notify_error": None}
    await store.write([base])
    await store.write([{**base, "notify_error": "sin conexión"}])
    await store.write([{**base, "notified_at": M0.isoformat()}])     # reintento con éxito: limpia el error
    await store.write([{**base, "ended_at": M1.isoformat(), "peak_people": 9}])
    await store.write([{**base, "peak_people": 4}])                  # repetición vieja: no pisa fin ni pico
    await store.close()
    rows = q(pg_dsn, "SELECT ended_at, peak_people, notified_at IS NOT NULL, notify_error FROM queue_alerts")
    assert rows == [(M1, 9, True, None)]


async def test_spool_survives_db_outage_and_replays_in_order(pg_dsn: str, tmp_path: Path) -> None:
    dead = f"postgresql://nadie:clave@127.0.0.1:{get_free_port()}/nada"
    spool = Spool(tmp_path / "spool")
    p = Persistence(spool, PgStore(dead, SITE, connect_timeout=2))
    p.submit(meta())
    p.submit([line_record("rule-00000001", "cam-00000001", M0, 1, 0)])
    p.submit([line_record("rule-00000001", "cam-00000001", M0, 4, 2)])  # versión posterior del mismo minuto
    assert await p.drain() == 0
    assert not p.ok and p.last_error and "clave" not in p.last_error
    assert spool.pending() == 3
    # Vuelve la base (otro DSN válido): se reenvía todo en orden y la última versión gana.
    p.store = PgStore(pg_dsn, SITE)
    assert await p.drain() == 3
    assert p.ok and spool.pending() == 0
    await p.store.close()
    assert q(pg_dsn, "SELECT count_in, count_out FROM line_counts_minute") == [(4, 2)]


async def test_corrupt_spool_file_is_quarantined(pg_dsn: str, tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool")
    (spool.dir / "00000000000000000001-1-000001.jsonl").write_text("{esto no es json\n", encoding="utf-8")
    spool.append([line_record("rule-00000001", "cam-00000001", M0, 2, 0)])
    p = Persistence(spool, PgStore(pg_dsn, SITE))
    assert await p.drain() == 1
    await p.store.close()  # type: ignore[union-attr]
    assert spool.pending() == 0 and len(list((spool.dir / "bad").iterdir())) == 1


def test_spool_limit_drops_oldest(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "s", max_files=3)
    for i in range(5):
        spool.append([{"type": "line", "n": i}])
    files = spool.files()
    assert len(files) == 3
    assert [json.loads(f.read_text())["n"] for f in files] == [2, 3, 4]


def test_without_dsn_data_waits_in_spool(tmp_path: Path) -> None:
    p = Persistence(Spool(tmp_path / "s"), None)
    p.submit([{"type": "line"}])
    assert p.spool.pending() == 1 and "VMS_PG_DSN" in p.last_error
