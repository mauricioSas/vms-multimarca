from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from vms.db.migrate import apply_migrations, available_migrations

pytestmark = pytest.mark.needs_postgres


def test_migrations_apply_once(pg_empty_dsn: str) -> None:
    first = apply_migrations(pg_empty_dsn)
    assert first == [v for v, _ in available_migrations()]
    assert apply_migrations(pg_empty_dsn) == []
    with psycopg.connect(pg_empty_dsn) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert {"sites", "site_cameras", "analytics_rules", "line_counts_minute", "zone_occupancy_minute",
            "queue_alerts", "site_heartbeats", "weekly_reports", "schema_migrations"} <= tables


def test_schema_constraints(pg_dsn: str) -> None:
    minute = datetime(2026, 10, 5, 9, 31, tzinfo=timezone.utc)
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("INSERT INTO sites (site_id, name) VALUES ('site-bcn-001', 'Tienda 1')")
        upsert = ("INSERT INTO line_counts_minute (site_id, rule_id, camera_id, minute, count_in, count_out) "
                  "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (site_id, rule_id, minute) "
                  "DO UPDATE SET count_in = EXCLUDED.count_in, count_out = EXCLUDED.count_out")
        conn.execute(upsert, ("site-bcn-001", "rule-1", "cam-1", minute, 5, 3))
        conn.execute(upsert, ("site-bcn-001", "rule-1", "cam-1", minute, 7, 4))  # reintento idempotente
        assert conn.execute("SELECT count_in, count_out FROM line_counts_minute").fetchone() == (7, 4)
        conn.execute("INSERT INTO queue_alerts (alert_id, site_id, rule_id, camera_id, started_at, peak_people,"
                     " threshold) VALUES (%s, 'site-bcn-001', 'rule-2', 'cam-2', now(), 8, 5)", (uuid.uuid4(),))
        conn.commit()

    bad_cases = [
        ("INSERT INTO sites (site_id, name) VALUES ('Tienda Mayúsculas', 'x')", ()),
        ("INSERT INTO line_counts_minute VALUES ('site-bcn-001','r','c', %s, 1, 1)",
         (minute.replace(second=10),)),
        ("INSERT INTO line_counts_minute VALUES ('site-bcn-001','r2','c', %s, -1, 0)", (minute,)),
        ("INSERT INTO line_counts_minute VALUES ('site-nope','r','c', %s, 1, 1)", (minute,)),
        ("INSERT INTO weekly_reports (site_id, week_start) VALUES ('site-bcn-001', '2026-10-07')", ()),
        ("INSERT INTO zone_occupancy_minute VALUES ('site-bcn-001','z','c', %s, 10, 1.5, 3, 61)", (minute,)),
    ]
    for sql, params in bad_cases:
        with psycopg.connect(pg_dsn) as conn, pytest.raises(psycopg.errors.IntegrityError):
            conn.execute(sql, params)  # type: ignore[arg-type]


def test_each_test_gets_a_fresh_database(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM sites").fetchone() == (0,)
