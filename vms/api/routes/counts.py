"""Exportación CSV de conteos de la tienda (CONTRATO §18.15). Dueño: B6.

`GET /api/analytics/counts.csv?from=&to=&bucket=hour|day` (O). Necesita `VMS_PG_DSN` (los conteos viven en
PostgreSQL); si no está, 503 `not_configured`. Columnas `tienda;fecha;hora;entradas;salidas;cola_media;cola_max`,
`;`, coma decimal, UTF-8 con BOM y fechas en la zona de la sede. Solo agregados.
"""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import NotConfigured, VmsError
from vms.ops.counts import fetch_rows, filename, parse_range, render_csv

from ..deps import Principal, get_state, require_operator
from ..security import client_ip
from ..state import AppState

log = logging.getLogger("vms.api.counts")
router = APIRouter(prefix="/api", tags=["counts"])


@router.get("/analytics/counts.csv")
async def counts_csv(request: Request, from_: Annotated[str | None, Query(alias="from")] = None,
                     to: str | None = None, bucket: Literal["hour", "day"] = "hour",
                     p: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    dsn = state.settings.pg_dsn
    if not dsn:
        raise NotConfigured("Los conteos se guardan en la base de datos central y este equipo no tiene "
                            "configurada la conexión (VMS_PG_DSN)")
    site = state.site()
    start, end = parse_range(from_, to, site.timezone, bucket)
    try:
        import psycopg
        async with await psycopg.AsyncConnection.connect(dsn.get_secret_value(), connect_timeout=5) as conn:
            rows = await fetch_rows(conn, site.id, site.name, site.timezone, start, end, bucket)
    except VmsError:
        raise
    except Exception as exc:  # noqa: BLE001 - sin detalles de conexión en la respuesta
        log.warning("No se pudieron leer los conteos: %s", type(exc).__name__)
        raise VmsError("No se pudo conectar con la base de datos de conteos", code="database_unavailable",
                       status=503) from exc
    audit("counts_export", user=p.username, ip=client_ip(request), site=site.id, bucket=bucket,
          **{"from": start.isoformat(), "to": end.isoformat()}, rows=len(rows))
    return Response(render_csv(rows, bucket), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename(site.id, start, end, site.timezone)}"',
        "Cache-Control": "no-store"})
