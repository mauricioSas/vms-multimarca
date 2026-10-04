"""Regresión (hallazgo de seguridad «un único rol vms_site para todas las tiendas»): con la migración
0002 y un rol por tienda, un PC de tienda solo puede leer y escribir las filas de SU sede."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from analytics.storage import PgStore, StoreError
from central.heartbeat import HeartbeatPayload, SiteInfo, record_heartbeat_sync
from vms.db.site_roles import (
    SiteRoleError,
    create_site_role,
    list_site_roles,
    revoke_site_role,
    role_name_for,
    site_dsn,
)

pytestmark = pytest.mark.needs_postgres

MINUTE = "2026-10-05T09:31:00+00:00"


def _as(dsn: str, role: str, password: str) -> str:
    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


@pytest.fixture
def two_sites(pg_dsn: str) -> Iterator[dict[str, Any]]:
    tag = uuid.uuid4().hex[:6]
    a, b = f"site-rls-a{tag}", f"site-rls-b{tag}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO sites (site_id, name) VALUES (%s, 'Tienda B')", (b,))
        conn.execute("INSERT INTO line_counts_minute (site_id, rule_id, camera_id, minute, count_in, count_out) "
                     "VALUES (%s, 'rule-door0001', 'cam-door0001', %s, 50, 40)", (b, MINUTE))
        role, pw = create_site_role(conn, a)
    try:
        yield {"a": a, "b": b, "role": role, "dsn_a": _as(pg_dsn, role, pw)}
    finally:
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            revoke_site_role(conn, a)


def line(rule: str = "rule-door0001") -> dict[str, Any]:
    return {"type": "line", "rule_id": rule, "camera_id": "cam-door0001", "minute": MINUTE, "count_in": 3,
            "count_out": 1}


async def test_site_role_writes_its_own_rows(two_sites: dict[str, Any], pg_dsn: str) -> None:
    store = PgStore(two_sites["dsn_a"], two_sites["a"], "Tienda A")
    try:
        await store.write([{"type": "site", "name": "Tienda A"}, line()])
    finally:
        await store.close()
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT count_in FROM line_counts_minute WHERE site_id=%s",
                         (two_sites["a"],)).fetchone() == (3,)


async def test_site_role_cannot_write_another_site(two_sites: dict[str, Any], pg_dsn: str) -> None:
    store = PgStore(two_sites["dsn_a"], two_sites["b"], "Tienda B pirata")   # VMS_SITE_ID ajeno
    try:
        with pytest.raises(StoreError, match="row-level security"):
            await store.write([line()])
    finally:
        await store.close()
    with psycopg.connect(pg_dsn) as c:   # los datos de B siguen intactos
        assert c.execute("SELECT count_in FROM line_counts_minute WHERE site_id=%s",
                         (two_sites["b"],)).fetchone() == (50,)
        assert c.execute("SELECT name FROM sites WHERE site_id=%s", (two_sites["b"],)).fetchone() == ("Tienda B",)


def test_site_role_reads_and_updates_only_its_rows(two_sites: dict[str, Any]) -> None:
    with psycopg.connect(two_sites["dsn_a"], autocommit=True) as c:
        assert c.execute("SELECT DISTINCT site_id FROM line_counts_minute").fetchall() == []
        assert c.execute("SELECT site_id FROM sites WHERE site_id=%s", (two_sites["b"],)).fetchall() == []
        cur = c.execute("UPDATE line_counts_minute SET count_in = 0 WHERE site_id=%s", (two_sites["b"],))
        assert cur.rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM line_counts_minute")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM site_db_roles")


def test_heartbeat_of_another_site_is_rejected(two_sites: dict[str, Any]) -> None:
    payload = HeartbeatPayload(status="ok", version="t")
    with psycopg.connect(two_sites["dsn_a"]) as c:
        record_heartbeat_sync(c, SiteInfo(id=two_sites["a"], name="Tienda A"), payload)
        c.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            record_heartbeat_sync(c, SiteInfo(id=two_sites["b"], name="X"), payload)


def test_unmapped_roles_still_see_everything(two_sites: dict[str, Any], pg_dsn: str) -> None:
    """El panel central y el informe semanal (roles sin sede) leen todas las tiendas, como antes."""
    with psycopg.connect(pg_dsn) as c:
        sites = {r[0] for r in c.execute("SELECT site_id FROM line_counts_minute")}
    assert two_sites["b"] in sites
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        assert (two_sites["role"], two_sites["a"]) in list_site_roles(c)


def test_role_names_and_dsn() -> None:
    assert role_name_for("site-bcn-001") == "vms_site_bcn_001"
    with pytest.raises(SiteRoleError):
        role_name_for("Tienda; DROP TABLE sites")
    dsn = site_dsn("postgresql://postgres@10.0.0.5:5432/vms", "vms_site_bcn_001", "s3cr3t")
    d = conninfo_to_dict(dsn)
    assert (d["user"], d["password"], d["host"], d["dbname"], d["sslmode"]) == (
        "vms_site_bcn_001", "s3cr3t", "10.0.0.5", "vms", "require")
    assert "IP-DEL-SERVIDOR" in site_dsn("postgresql://postgres@localhost/vms", "r", "p")
