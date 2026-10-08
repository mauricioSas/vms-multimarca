"""El PostgreSQL de pruebas tiene zonas horarias y corre en UTC, también en Windows.

Regresión: la rueda de Windows de pgserver no trae `share/postgresql/timezone`; `SET TIME ZONE 'UTC'` (lo hace
cada conexión del panel central), `AT TIME ZONE 'Europe/Madrid'` y `pg_timezone_names` fallaban y con ellos casi
todo central/, analytics/ y db/ en un PC con Windows (tools/pgserver_tz.py).
"""
from __future__ import annotations

import os
from datetime import datetime

import psycopg
import pytest

pytestmark = pytest.mark.needs_postgres


def test_time_zones_work_like_on_linux(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        # Primero lo que fallaba de verdad («time zone ... not recognized»); la zona del servidor, al final
        c.execute("SET TIME ZONE 'UTC'")
        names = c.execute("SELECT count(*) FROM pg_timezone_names "
                          "WHERE name IN ('Europe/Madrid', 'Atlantic/Canary', 'UTC')").fetchone()
        assert names == (3,)
        # Cambio de hora del 25-oct-2026 en Madrid: 00:30Z y 01:30Z son las dos «02:30» locales
        both = c.execute("SELECT '2026-10-25 00:30+00'::timestamptz AT TIME ZONE 'Europe/Madrid', "
                         "'2026-10-25 01:30+00'::timestamptz AT TIME ZONE 'Europe/Madrid'").fetchone()
        assert both == (datetime(2026, 10, 25, 2, 30), datetime(2026, 10, 25, 2, 30))
        canary = c.execute("SELECT '2026-10-07 10:30+00'::timestamptz AT TIME ZONE 'Atlantic/Canary'").fetchone()
        assert canary == (datetime(2026, 10, 7, 11, 30),)
    if not os.environ.get("VMS_TEST_PG_DSN"):  # el embebido (pgserver), no un servidor que ponga la persona
        with psycopg.connect(pg_dsn, autocommit=True) as c:  # conexión nueva: la de arriba ya hizo SET TIME ZONE
            assert c.execute("SHOW timezone").fetchone() == ("UTC",), "igual en cualquier PC y en la CI"
