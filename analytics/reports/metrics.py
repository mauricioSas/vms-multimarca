"""Cifras del informe semanal calculadas en SQL (fuente de verdad del informe).

Todo se calcula en la zona horaria de la tienda (p. ej. Europe/Madrid): «la hora punta es a las
18 h» significa las 18 h locales, aunque en la base los minutos estén en UTC.

Qué se calcula:
- Puerta: entradas/salidas de la semana, por día y por hora del día, hora punta, día de más
  afluencia, comparación con la semana anterior y cobertura (minutos con datos).
- Colas: por zona, ocupación media y máxima, minutos por encima del umbral, ocupación media por
  franja horaria; alertas (número, duración media/máxima, pico de personas) y comparación.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import psycopg

WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MINUTES_PER_WEEK = 7 * 24 * 60

# Límites de la semana local convertidos a UTC dentro de la propia consulta.
_RANGE = ("minute >= (%(ws)s::date)::timestamp AT TIME ZONE %(tz)s "
          "AND minute < ((%(ws)s::date + 7))::timestamp AT TIME ZONE %(tz)s")


def _pct(now: float, before: float) -> float | None:
    if not before:
        return None
    return round((now - before) * 100.0 / before, 1)


async def _rows(conn: psycopg.AsyncConnection[Any], sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    cur = await conn.execute(sql, params)  # type: ignore[arg-type]
    return list(await cur.fetchall())


async def site_info(conn: psycopg.AsyncConnection[Any], site_id: str) -> dict[str, Any] | None:
    rows = await _rows(conn, "SELECT site_id, name, code, timezone FROM sites WHERE site_id = %(s)s", {"s": site_id})
    if not rows:
        return None
    sid, name, code, tz = rows[0]
    return {"id": sid, "name": name, "code": code, "timezone": tz}


async def _door_totals(conn: psycopg.AsyncConnection[Any], p: dict[str, Any]) -> tuple[int, int, int]:
    rows = await _rows(conn, f"""
        SELECT COALESCE(SUM(count_in), 0), COALESCE(SUM(count_out), 0), COUNT(DISTINCT minute)
        FROM line_counts_minute WHERE site_id = %(s)s AND {_RANGE}""", p)
    a, b, c = rows[0]
    return int(a), int(b), int(c)


async def _alert_summary(conn: psycopg.AsyncConnection[Any], p: dict[str, Any]) -> dict[str, Any]:
    rows = await _rows(conn, """
        SELECT COUNT(*),
               COALESCE(SUM(EXTRACT(EPOCH FROM (ended_at - started_at))) FILTER (WHERE ended_at IS NOT NULL), 0),
               COALESCE(AVG(EXTRACT(EPOCH FROM (ended_at - started_at))) FILTER (WHERE ended_at IS NOT NULL), 0),
               COALESCE(MAX(EXTRACT(EPOCH FROM (ended_at - started_at))), 0),
               COALESCE(MAX(peak_people), 0)
        FROM queue_alerts
        WHERE site_id = %(s)s AND started_at >= (%(ws)s::date)::timestamp AT TIME ZONE %(tz)s
          AND started_at < ((%(ws)s::date + 7))::timestamp AT TIME ZONE %(tz)s""", p)
    n, total_s, avg_s, max_s, peak = rows[0]
    return {"count": int(n), "total_minutes": round(float(total_s) / 60, 1),
            "avg_minutes": round(float(avg_s) / 60, 1), "max_minutes": round(float(max_s) / 60, 1),
            "max_peak_people": int(peak)}


async def compute_metrics(conn: psycopg.AsyncConnection[Any], site_id: str, week_start: date) -> dict[str, Any]:
    """Cifras de la semana ISO que empieza en `week_start` (lunes)."""
    if week_start.isoweekday() != 1:
        raise ValueError("week_start debe ser lunes")
    site = await site_info(conn, site_id)
    if site is None:
        raise LookupError(f"La sede {site_id} no existe en la base de datos")
    tz = site["timezone"] or "Europe/Madrid"
    prev = week_start - timedelta(days=7)
    p = {"s": site_id, "ws": week_start, "tz": tz}
    pp = {"s": site_id, "ws": prev, "tz": tz}

    # ------------------------------------------------------------------ puerta
    total_in, total_out, door_minutes = await _door_totals(conn, p)
    prev_in, prev_out, _ = await _door_totals(conn, pp)
    by_day_rows = await _rows(conn, f"""
        SELECT (minute AT TIME ZONE %(tz)s)::date AS d, SUM(count_in), SUM(count_out)
        FROM line_counts_minute WHERE site_id = %(s)s AND {_RANGE} GROUP BY d ORDER BY d""", p)
    day_map = {r[0]: (int(r[1]), int(r[2])) for r in by_day_rows}
    by_day = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        cin, cout = day_map.get(d, (0, 0))
        by_day.append({"date": d.isoformat(), "weekday": WEEKDAYS_ES[i], "in": cin, "out": cout})
    by_hour_rows = await _rows(conn, f"""
        SELECT EXTRACT(HOUR FROM minute AT TIME ZONE %(tz)s)::int AS h, SUM(count_in), SUM(count_out)
        FROM line_counts_minute WHERE site_id = %(s)s AND {_RANGE} GROUP BY h ORDER BY h""", p)
    hour_map = {int(r[0]): (int(r[1]), int(r[2])) for r in by_hour_rows}
    by_hour = [{"hour": h, "in": hour_map.get(h, (0, 0))[0], "out": hour_map.get(h, (0, 0))[1]} for h in range(24)]
    peak_hour = max(by_hour, key=lambda x: x["in"]) if total_in else None
    busiest_day = max(by_day, key=lambda x: x["in"]) if total_in else None
    quietest_day = min((d for d in by_day if d["in"] > 0), key=lambda x: x["in"], default=None)
    peak_by_day_rows = await _rows(conn, f"""
        SELECT d, h, n FROM (
          SELECT (minute AT TIME ZONE %(tz)s)::date AS d, EXTRACT(HOUR FROM minute AT TIME ZONE %(tz)s)::int AS h,
                 SUM(count_in) AS n,
                 ROW_NUMBER() OVER (PARTITION BY (minute AT TIME ZONE %(tz)s)::date ORDER BY SUM(count_in) DESC,
                                    EXTRACT(HOUR FROM minute AT TIME ZONE %(tz)s)::int) AS rn
          FROM line_counts_minute WHERE site_id = %(s)s AND {_RANGE}
          GROUP BY 1, 2) t WHERE rn = 1 AND n > 0 ORDER BY d""", p)
    rules_rows = await _rows(conn, f"""
        SELECT l.rule_id, COALESCE(r.name, l.rule_id), l.camera_id, SUM(l.count_in), SUM(l.count_out)
        FROM line_counts_minute l LEFT JOIN analytics_rules r ON r.site_id = l.site_id AND r.rule_id = l.rule_id
        WHERE l.site_id = %(s)s AND {_RANGE.replace('minute', 'l.minute')}
        GROUP BY 1, 2, 3 ORDER BY 4 DESC""", p)

    doors = {
        "total_in": total_in, "total_out": total_out,
        "previous_total_in": prev_in, "previous_total_out": prev_out,
        "change_in_pct": _pct(total_in, prev_in),
        "daily_avg_in": round(total_in / 7, 1),
        "by_day": by_day, "by_hour": by_hour,
        "peak_hour": {"hour": peak_hour["hour"], "in": peak_hour["in"]} if peak_hour else None,
        "busiest_day": {k: busiest_day[k] for k in ("date", "weekday", "in")} if busiest_day else None,
        "quietest_day": {k: quietest_day[k] for k in ("date", "weekday", "in")} if quietest_day else None,
        "peak_hour_by_day": [{"date": r[0].isoformat(), "weekday": WEEKDAYS_ES[r[0].weekday()], "hour": int(r[1]),
                              "in": int(r[2])} for r in peak_by_day_rows],
        "lines": [{"rule_id": r[0], "name": r[1], "camera_id": r[2], "in": int(r[3]), "out": int(r[4])}
                  for r in rules_rows],
        "minutes_with_data": door_minutes,
        "coverage_pct": round(door_minutes * 100.0 / MINUTES_PER_WEEK, 1),
    }

    # ------------------------------------------------------------------ colas
    zone_rows = await _rows(conn, f"""
        SELECT z.rule_id, COALESCE(r.name, z.rule_id), z.camera_id,
               SUM(z.avg_people * z.samples) / NULLIF(SUM(z.samples), 0), MAX(z.max_people),
               SUM(z.seconds_over_threshold), COUNT(*), (r.config ->> 'alert_threshold')::int
        FROM zone_occupancy_minute z
        LEFT JOIN analytics_rules r ON r.site_id = z.site_id AND r.rule_id = z.rule_id
        WHERE z.site_id = %(s)s AND {_RANGE.replace('minute', 'z.minute')}
        GROUP BY 1, 2, 3, 8 ORDER BY 2""", p)
    zones = []
    for rid, name, cam, avg, mx, over_s, minutes, threshold in zone_rows:
        hour_rows = await _rows(conn, f"""
            SELECT EXTRACT(HOUR FROM minute AT TIME ZONE %(tz)s)::int AS h,
                   SUM(avg_people * samples) / NULLIF(SUM(samples), 0), MAX(max_people)
            FROM zone_occupancy_minute WHERE site_id = %(s)s AND rule_id = %(r)s AND {_RANGE}
            GROUP BY h ORDER BY h""", {**p, "r": rid})
        hours = [{"hour": int(h), "avg_people": round(float(a or 0), 1), "max_people": int(m)} for h, a, m in hour_rows]
        busiest = max(hours, key=lambda x: x["avg_people"]) if hours else None
        zones.append({"rule_id": rid, "name": name, "camera_id": cam, "threshold": threshold,
                      "avg_people": round(float(avg or 0), 1), "max_people": int(mx or 0),
                      "minutes_over_threshold": round(float(over_s or 0) / 60, 1),
                      "minutes_with_data": int(minutes), "by_hour": hours,
                      "busiest_hour": busiest})
    alerts = await _alert_summary(conn, p)
    prev_alerts = await _alert_summary(conn, pp)
    alert_zone_rows = await _rows(conn, """
        SELECT a.rule_id, COALESCE(r.name, a.rule_id), COUNT(*)
        FROM queue_alerts a LEFT JOIN analytics_rules r ON r.site_id = a.site_id AND r.rule_id = a.rule_id
        WHERE a.site_id = %(s)s AND a.started_at >= (%(ws)s::date)::timestamp AT TIME ZONE %(tz)s
          AND a.started_at < ((%(ws)s::date + 7))::timestamp AT TIME ZONE %(tz)s
        GROUP BY 1, 2 ORDER BY 3 DESC""", p)
    alerts["previous_count"] = prev_alerts["count"]
    alerts["by_zone"] = [{"rule_id": r[0], "name": r[1], "count": int(r[2])} for r in alert_zone_rows]

    iso = week_start.isocalendar()
    return {
        "site": site,
        "week": {"start": week_start.isoformat(), "end": (week_start + timedelta(days=6)).isoformat(),
                 "iso_week": f"{iso.year}-W{iso.week:02d}", "previous_start": prev.isoformat()},
        "doors": doors,
        "queues": {"zones": zones, "alerts": alerts},
    }
