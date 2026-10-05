"""Exportación CSV de conteos para Excel (CONTRATO §18.15 y §18.16).

Columnas `tienda;fecha;hora;entradas;salidas;cola_media;cola_max`, separador `;`, coma decimal, UTF-8 **con
BOM** y fechas en la zona de la sede. Solo agregados (sin datos personales): sumas de cruces de las líneas
de puerta y media/máximo de personas en las zonas de cola por hora o por día. Lo usan la tienda
(`GET /api/analytics/counts.csv`) y el panel central (`central/ops.py`).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vms.core.errors import ValidationFailed

Bucket = Literal["hour", "day"]
MAX_DAYS = {"hour": 62, "day": 400}

_SQL = """
WITH lc AS (
    SELECT date_trunc(%(bucket)s, minute AT TIME ZONE %(tz)s) AS b, sum(count_in) AS cin, sum(count_out) AS cout
    FROM line_counts_minute
    WHERE site_id = %(site)s AND minute >= %(start)s AND minute < %(end)s
    GROUP BY 1
), zo AS (
    SELECT date_trunc(%(bucket)s, minute AT TIME ZONE %(tz)s) AS b, avg(avg_people) AS qavg, max(max_people) AS qmax
    FROM zone_occupancy_minute
    WHERE site_id = %(site)s AND minute >= %(start)s AND minute < %(end)s
    GROUP BY 1
)
SELECT coalesce(lc.b, zo.b) AS bucket_local, coalesce(lc.cin, 0) AS cin, coalesce(lc.cout, 0) AS cout,
       zo.qavg AS qavg, zo.qmax AS qmax
FROM lc FULL OUTER JOIN zo ON lc.b = zo.b
ORDER BY 1
"""


@dataclass(frozen=True)
class CountRow:
    site: str
    local: datetime            # inicio del intervalo en hora local de la sede (sin zona)
    entries: int
    exits: int
    queue_avg: float | None
    queue_max: int | None


def tz_of(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailed("Zona horaria de la sede desconocida") from exc


def parse_range(from_: str | None, to: str | None, tz_name: str, bucket: str, *,
                today: date | None = None) -> tuple[datetime, datetime]:
    """Fechas `AAAA-MM-DD` (incluidas) o fechas y horas ISO. Por defecto, hoy en la zona de la sede."""
    if bucket not in MAX_DAYS:
        raise ValidationFailed("El intervalo debe ser «hour» o «day»")
    tz = tz_of(tz_name)
    today = today or datetime.now(tz).date()

    def point(value: str | None, default: date, end: bool) -> datetime:
        if not value:
            d = default + timedelta(days=1) if end else default
            return datetime.combine(d, time(0), tz)
        try:
            if len(value) == 10:
                d = date.fromisoformat(value)
                return datetime.combine(d + timedelta(days=1) if end else d, time(0), tz)
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=tz)
        except ValueError as exc:
            raise ValidationFailed("Fecha no válida (usa AAAA-MM-DD)",
                                   details={"fields": [{"loc": ["to" if end else "from"], "msg": "Fecha no válida"}]}) \
                from exc

    start = point(from_, today, False)
    end = point(to, start.astimezone(tz).date() if from_ and not to else today, True)
    if end <= start:
        raise ValidationFailed("El final debe ser posterior al inicio")
    if end - start > timedelta(days=MAX_DAYS[bucket]):
        raise ValidationFailed(f"Como mucho {MAX_DAYS[bucket]} días con el intervalo «{bucket}»")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def fetch_rows(conn: Any, site_id: str, site_label: str, tz_name: str, start: datetime, end: datetime,
                     bucket: Bucket) -> list[CountRow]:
    tz_of(tz_name)
    async with conn.cursor() as cur:
        await cur.execute(_SQL, {"bucket": bucket, "tz": tz_name, "site": site_id, "start": start, "end": end})
        rows = await cur.fetchall()
    out = []
    for r in rows:
        rec = r if isinstance(r, dict) else dict(zip(("bucket_local", "cin", "cout", "qavg", "qmax"), r, strict=False))
        out.append(CountRow(site=site_label, local=rec["bucket_local"], entries=int(rec["cin"] or 0),
                            exits=int(rec["cout"] or 0),
                            queue_avg=None if rec["qavg"] is None else float(rec["qavg"]),
                            queue_max=None if rec["qmax"] is None else int(rec["qmax"])))
    return out


def _dec(v: float | None, digits: int = 1) -> str:
    return "" if v is None else f"{v:.{digits}f}".replace(".", ",")


def render_csv(rows: list[CountRow], bucket: Bucket) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["tienda", "fecha", "hora", "entradas", "salidas", "cola_media", "cola_max"])
    for r in rows:
        w.writerow([r.site, r.local.strftime("%d/%m/%Y"), r.local.strftime("%H:%M") if bucket == "hour" else "",
                    r.entries, r.exits, _dec(r.queue_avg), "" if r.queue_max is None else r.queue_max])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def filename(prefix: str, start: datetime, end: datetime, tz_name: str) -> str:
    tz = tz_of(tz_name)
    a = start.astimezone(tz).date()
    b = (end - timedelta(seconds=1)).astimezone(tz).date()
    return f"conteos_{prefix}_{a.isoformat()}_{b.isoformat()}.csv"
