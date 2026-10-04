"""Consultas de solo lectura del panel central sobre la PostgreSQL común (CONTRATO §7.1).

Todas las fechas se devuelven como `datetime` con zona (UTC). Los cortes por día, hora y
semana se calculan en la zona horaria de cada sede (columna `sites.timezone`), en SQL, para
que «hoy» sea el día de la tienda y no el del servidor. `now` se puede inyectar (pruebas).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("central.db")

Bucket = Literal["hour", "day"]
RangeName = Literal["today", "yesterday", "week", "prev_week", "last7", "last30"]
RANGE_NAMES: tuple[str, ...] = ("today", "yesterday", "week", "prev_week", "last7", "last30")
MAX_SPAN = {"hour": timedelta(days=31), "day": timedelta(days=400)}

# Expresiones (inicio, fin) en hora LOCAL de la sede; NULL en el fin = «ahora».
_LOCAL_DAY = "date_trunc('day', %(now)s::timestamptz AT TIME ZONE %(tz)s)"
_LOCAL_WEEK = "date_trunc('week', %(now)s::timestamptz AT TIME ZONE %(tz)s)"
_RANGES: dict[str, tuple[str, str | None]] = {
    "today": (_LOCAL_DAY, None),
    "yesterday": (f"{_LOCAL_DAY} - interval '1 day'", _LOCAL_DAY),
    "week": (_LOCAL_WEEK, None),
    "prev_week": (f"{_LOCAL_WEEK} - interval '7 days'", _LOCAL_WEEK),
    "last7": (f"{_LOCAL_DAY} - interval '6 days'", None),
    "last30": (f"{_LOCAL_DAY} - interval '29 days'", None),
}


async def _configure(conn: AsyncConnection[Any]) -> None:
    await conn.execute("SET TIME ZONE 'UTC'")


def make_pool(dsn: str, max_size: int = 10) -> AsyncConnectionPool[AsyncConnection[dict[str, Any]]]:
    """Pool asíncrono (se abre con `await pool.open(wait=False)` en el arranque)."""
    return AsyncConnectionPool(
        dsn, min_size=1, max_size=max_size, open=False, configure=_configure,
        kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 10},
        name="central",
    )


# =========================================================================== utilidades
async def fetch_all(conn: AsyncConnection[Any], sql: str, params: dict[str, Any] | tuple[Any, ...] = ()) \
        -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)  # type: ignore[arg-type]
    return list(await cur.fetchall())


async def fetch_one(conn: AsyncConnection[Any], sql: str, params: dict[str, Any] | tuple[Any, ...] = ()) \
        -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)  # type: ignore[arg-type]
    return await cur.fetchone()


async def timezone_exists(conn: AsyncConnection[Any], tz: str) -> bool:
    return await fetch_one(conn, "SELECT 1 AS ok FROM pg_timezone_names WHERE name = %s", (tz,)) is not None


async def resolve_range(conn: AsyncConnection[Any], name: str, tz: str, now: datetime) -> tuple[datetime, datetime]:
    """Rango con nombre → (inicio, fin) en UTC, con los cortes de día/semana en la zona `tz`."""
    if name not in _RANGES:
        raise ValueError(f"Rango desconocido: {name}")
    start_expr, end_expr = _RANGES[name]
    end_sql = f"({end_expr}) AT TIME ZONE %(tz)s" if end_expr else "%(now)s::timestamptz"
    row = await fetch_one(conn, f"SELECT ({start_expr}) AT TIME ZONE %(tz)s AS start, {end_sql} AS end",
                          {"now": now, "tz": tz})
    assert row is not None
    return row["start"], row["end"]


# =========================================================================== sedes
_SITES_SQL = f"""
WITH s AS (
    SELECT site_id, name, code, timezone, active,
           ({_LOCAL_DAY.replace('%(tz)s', 'sites.timezone')}) AT TIME ZONE sites.timezone AS day_start,
           ({_LOCAL_WEEK.replace('%(tz)s', 'sites.timezone')}) AT TIME ZONE sites.timezone AS week_start
    FROM sites
    WHERE (%(site_id)s::text IS NULL OR site_id = %(site_id)s::text)
      AND (active OR %(site_id)s::text IS NOT NULL)
)
SELECT s.site_id, s.name, s.code, s.timezone, s.active, s.day_start, s.week_start,
       h.last_seen, h.hostname, h.version, h.status AS reported_status, h.payload,
       COALESCE(t.cin, 0)::bigint  AS today_in,
       COALESCE(t.cout, 0)::bigint AS today_out,
       COALESCE(w.cin, 0)::bigint  AS week_in,
       COALESCE(w.cout, 0)::bigint AS week_out,
       COALESCE(pw.cin, 0)::bigint AS prev_week_in,
       COALESCE(a.today, 0)::bigint AS alerts_today,
       COALESCE(a.week, 0)::bigint  AS alerts_week,
       COALESCE(a.open, 0)::bigint  AS alerts_open,
       (SELECT max(r.week_start) FROM weekly_reports r WHERE r.site_id = s.site_id) AS last_report_week,
       (SELECT count(*) FROM site_cameras c WHERE c.site_id = s.site_id)::bigint AS cameras_known
FROM s
LEFT JOIN site_heartbeats h ON h.site_id = s.site_id
LEFT JOIN LATERAL (
    SELECT sum(count_in) AS cin, sum(count_out) AS cout FROM line_counts_minute l
    WHERE l.site_id = s.site_id AND l.minute >= s.day_start AND l.minute < %(now)s::timestamptz) t ON true
LEFT JOIN LATERAL (
    SELECT sum(count_in) AS cin, sum(count_out) AS cout FROM line_counts_minute l
    WHERE l.site_id = s.site_id AND l.minute >= s.week_start AND l.minute < %(now)s::timestamptz) w ON true
LEFT JOIN LATERAL (
    SELECT sum(count_in) AS cin FROM line_counts_minute l
    WHERE l.site_id = s.site_id AND l.minute >= s.week_start - interval '7 days'
      AND l.minute < %(now)s::timestamptz - interval '7 days') pw ON true
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE q.started_at >= s.day_start) AS today,
           count(*) FILTER (WHERE q.started_at >= s.week_start) AS week,
           count(*) FILTER (WHERE q.ended_at IS NULL) AS open
    FROM queue_alerts q
    WHERE q.site_id = s.site_id AND q.started_at < %(now)s::timestamptz
      AND (q.started_at >= s.week_start OR q.ended_at IS NULL)) a ON true
ORDER BY s.name, s.site_id
"""


async def list_sites(conn: AsyncConnection[Any], now: datetime, site_id: str | None = None) -> list[dict[str, Any]]:
    return await fetch_all(conn, _SITES_SQL, {"now": now, "site_id": site_id})


async def get_site_row(conn: AsyncConnection[Any], site_id: str) -> dict[str, Any] | None:
    return await fetch_one(conn, "SELECT site_id, name, code, timezone, active FROM sites WHERE site_id = %s",
                           (site_id,))


# =========================================================================== series por sede
def _bucket_series_sql(table_select: str, join_cond_extra: str = "") -> str:
    """Serie con TODOS los tramos (los vacíos con 0) en hora local de la sede."""
    return f"""
WITH b AS (
    SELECT DISTINCT (g AT TIME ZONE %(tz)s) AS bucket_start,
           ((g + ('1 ' || %(bucket)s)::interval) AT TIME ZONE %(tz)s) AS bucket_end
    FROM generate_series(date_trunc(%(bucket)s, %(start)s::timestamptz AT TIME ZONE %(tz)s),
                         (%(end)s::timestamptz AT TIME ZONE %(tz)s) - interval '1 microsecond',
                         ('1 ' || %(bucket)s)::interval) AS g
)
SELECT b.bucket_start, {table_select}
FROM b
LEFT JOIN {{table}} x ON x.site_id = %(site_id)s
     AND x.minute >= b.bucket_start AND x.minute < b.bucket_end
     AND x.minute >= %(start)s::timestamptz AND x.minute < %(end)s::timestamptz
     AND (%(rule_id)s::text IS NULL OR x.rule_id = %(rule_id)s::text) {join_cond_extra}
GROUP BY b.bucket_start
ORDER BY b.bucket_start
"""


_COUNTS_SQL = _bucket_series_sql(
    "COALESCE(sum(x.count_in), 0)::bigint AS count_in, COALESCE(sum(x.count_out), 0)::bigint AS count_out"
).replace("{table}", "line_counts_minute")

_OCCUPANCY_SQL = _bucket_series_sql(
    "round(avg(x.avg_people)::numeric, 2)::float8 AS avg_people, max(x.max_people) AS max_people, "
    "COALESCE(sum(x.seconds_over_threshold), 0)::bigint AS seconds_over_threshold, "
    "count(x.minute)::bigint AS minutes"
).replace("{table}", "zone_occupancy_minute")


def check_span(start: datetime, end: datetime, bucket: str) -> None:
    if end <= start:
        raise ValueError("El final del rango debe ser posterior al inicio")
    if end - start > MAX_SPAN[bucket]:
        raise ValueError(f"Rango demasiado largo para tramos de {'hora' if bucket == 'hour' else 'día'}")


async def site_counts(conn: AsyncConnection[Any], site_id: str, tz: str, start: datetime, end: datetime,
                      bucket: Bucket, rule_id: str | None = None) -> list[dict[str, Any]]:
    check_span(start, end, bucket)
    return await fetch_all(conn, _COUNTS_SQL, {"site_id": site_id, "tz": tz, "start": start, "end": end,
                                               "bucket": bucket, "rule_id": rule_id})


async def site_occupancy(conn: AsyncConnection[Any], site_id: str, tz: str, start: datetime, end: datetime,
                         bucket: Bucket, rule_id: str | None = None) -> list[dict[str, Any]]:
    check_span(start, end, bucket)
    return await fetch_all(conn, _OCCUPANCY_SQL, {"site_id": site_id, "tz": tz, "start": start, "end": end,
                                                  "bucket": bucket, "rule_id": rule_id})


_ALERT_SECONDS = ("extract(epoch FROM (COALESCE(q.ended_at, LEAST(%(now)s::timestamptz, %(end)s::timestamptz))"
                  " - q.started_at))")


async def site_queue_alerts(conn: AsyncConnection[Any], site_id: str, start: datetime, end: datetime,
                            now: datetime, limit: int = 200) -> list[dict[str, Any]]:
    return await fetch_all(conn, f"""
        SELECT q.alert_id::text AS alert_id, q.rule_id, r.name AS rule_name, q.camera_id,
               c.name AS camera_name, q.started_at, q.ended_at, q.peak_people, q.threshold,
               q.notified_at, (q.notify_error IS NOT NULL AND q.notify_error <> '') AS notify_failed,
               GREATEST({_ALERT_SECONDS}, 0)::float8 AS duration_s
        FROM queue_alerts q
        LEFT JOIN analytics_rules r ON r.site_id = q.site_id AND r.rule_id = q.rule_id
        LEFT JOIN site_cameras c ON c.site_id = q.site_id AND c.camera_id = q.camera_id
        WHERE q.site_id = %(site_id)s AND q.started_at >= %(start)s AND q.started_at < %(end)s
        ORDER BY q.started_at DESC
        LIMIT %(limit)s""", {"site_id": site_id, "start": start, "end": end, "now": now, "limit": limit})


async def site_rules(conn: AsyncConnection[Any], site_id: str) -> list[dict[str, Any]]:
    return await fetch_all(conn, """
        SELECT r.rule_id, r.camera_id, c.name AS camera_name, r.kind, r.name, r.active, r.updated_at
        FROM analytics_rules r
        LEFT JOIN site_cameras c ON c.site_id = r.site_id AND c.camera_id = r.camera_id
        WHERE r.site_id = %s ORDER BY r.kind, r.name""", (site_id,))


async def site_cameras(conn: AsyncConnection[Any], site_id: str) -> list[dict[str, Any]]:
    return await fetch_all(conn, "SELECT camera_id, name, updated_at FROM site_cameras WHERE site_id = %s "
                                 "ORDER BY name", (site_id,))


# =========================================================================== multi-sede
async def compare_sites(conn: AsyncConnection[Any], start: datetime, end: datetime, now: datetime) \
        -> list[dict[str, Any]]:
    return await fetch_all(conn, f"""
        SELECT s.site_id, s.name, s.code,
               COALESCE(l.cin, 0)::bigint AS entries, COALESCE(l.cout, 0)::bigint AS exits,
               COALESCE(a.n, 0)::bigint AS alerts, COALESCE(a.secs, 0)::float8 AS alert_seconds,
               z.avg_people, z.max_people
        FROM sites s
        LEFT JOIN LATERAL (
            SELECT sum(count_in) AS cin, sum(count_out) AS cout FROM line_counts_minute x
            WHERE x.site_id = s.site_id AND x.minute >= %(start)s AND x.minute < %(end)s) l ON true
        LEFT JOIN LATERAL (
            SELECT count(*) AS n, sum(GREATEST({_ALERT_SECONDS}, 0)) AS secs FROM queue_alerts q
            WHERE q.site_id = s.site_id AND q.started_at >= %(start)s AND q.started_at < %(end)s) a ON true
        LEFT JOIN LATERAL (
            SELECT round(avg(avg_people)::numeric, 2)::float8 AS avg_people, max(max_people) AS max_people
            FROM zone_occupancy_minute x
            WHERE x.site_id = s.site_id AND x.minute >= %(start)s AND x.minute < %(end)s) z ON true
        WHERE s.active
        ORDER BY entries DESC, s.name""", {"start": start, "end": end, "now": now})


async def top_queues(conn: AsyncConnection[Any], start: datetime, end: datetime, now: datetime,
                     limit: int = 10) -> list[dict[str, Any]]:
    return await fetch_all(conn, f"""
        SELECT q.site_id, s.name AS site_name, q.rule_id, max(r.name) AS rule_name,
               max(q.camera_id) AS camera_id, max(c.name) AS camera_name,
               count(*)::bigint AS alerts,
               sum(GREATEST({_ALERT_SECONDS}, 0))::float8 AS alert_seconds,
               max(q.peak_people) AS peak_people, max(q.started_at) AS last_alert
        FROM queue_alerts q
        JOIN sites s ON s.site_id = q.site_id
        LEFT JOIN analytics_rules r ON r.site_id = q.site_id AND r.rule_id = q.rule_id
        LEFT JOIN site_cameras c ON c.site_id = q.site_id AND c.camera_id = q.camera_id
        WHERE q.started_at >= %(start)s AND q.started_at < %(end)s
        GROUP BY q.site_id, s.name, q.rule_id
        ORDER BY alerts DESC, alert_seconds DESC, q.site_id
        LIMIT %(limit)s""", {"start": start, "end": end, "now": now, "limit": limit})


# =========================================================================== informes
async def list_reports(conn: AsyncConnection[Any], site_id: str) -> list[dict[str, Any]]:
    return await fetch_all(conn, """
        SELECT site_id, week_start, generated_at, status, provider, model,
               (error IS NOT NULL AND error <> '') AS has_error
        FROM weekly_reports WHERE site_id = %s ORDER BY week_start DESC""", (site_id,))


async def get_report(conn: AsyncConnection[Any], site_id: str, week_start: date) -> dict[str, Any] | None:
    return await fetch_one(conn, """
        SELECT r.site_id, s.name AS site_name, r.week_start, r.generated_at, r.status, r.provider, r.model,
               r.metrics, r.body_markdown, r.error
        FROM weekly_reports r JOIN sites s ON s.site_id = r.site_id
        WHERE r.site_id = %s AND r.week_start = %s""", (site_id, week_start))


async def latest_reports(conn: AsyncConnection[Any]) -> list[dict[str, Any]]:
    return await fetch_all(conn, """
        SELECT DISTINCT ON (r.site_id) r.site_id, s.name AS site_name, r.week_start, r.generated_at,
               r.status, r.provider, r.model
        FROM weekly_reports r JOIN sites s ON s.site_id = r.site_id
        ORDER BY r.site_id, r.week_start DESC""")
