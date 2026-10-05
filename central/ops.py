"""Panel central: «tiendas con problemas hoy», salud por sede y CSV (CONTRATO §18.16). Dueño: B6.

Todo sale del último latido de cada sede (`site_heartbeats.payload.health`, que construye el backend de la
tienda con `vms.ops.heartbeat`): el panel no habla con las tiendas ni sale a Internet.

| Método y ruta | Rol |
|---|---|
| `GET /ops` (página «Tiendas con problemas hoy»; imprimible para el parte en PDF) | sesión |
| `GET /api/ops/problems?date=` | O |
| `GET /api/ops/problems.csv?date=` | O |
| `GET /api/ops/sites/{site_id}/health` | O |
| `GET /api/sites/{site_id}/counts.csv?from=&to=&bucket=` | O |
| `GET /api/counts.csv?sites=a,b&from=&to=&bucket=` | O |

`date`: el latido lleva el informe del día de la tienda (`report_date`); si se pide otro día y la sede no tiene
datos de ese día, sale como «sin datos de ese día» en vez de inventarlos.
"""
# Sin «from __future__ import annotations»: FastAPI debe resolver las anotaciones locales (Sess, Conn) al definir
# las rutas dentro de build_router (igual que central/app.py).
import csv
import io
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pydantic_core

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response

from vms.core.errors import NotFoundError, ValidationFailed
from vms.core.naming import is_valid_id
from vms.ops.counts import Bucket, CountRow, fetch_rows, filename, parse_range, render_csv
from vms.ops.csvsafe import text_cell

from .extensions import CentralDeps

WEB_DIR = Path(__file__).resolve().parent / "web"
STALE_AFTER_S = 15 * 60
SEVERITY_ORDER = {"critical": 0, "nodata": 1, "warning": 2, "ok": 3}

_SQL = """
SELECT s.site_id, s.name, s.code, s.timezone, h.last_seen, h.status AS reported_status,
       h.payload -> 'health' AS health, h.payload -> 'evidence_key' AS evidence_key
FROM sites s LEFT JOIN site_heartbeats h ON h.site_id = s.site_id
WHERE s.active AND (%(site_id)s::text IS NULL OR s.site_id = %(site_id)s)
ORDER BY s.site_id
"""


def _row_dict(r: Any) -> dict[str, Any]:
    if isinstance(r, dict):
        return r
    keys = ("site_id", "name", "code", "timezone", "last_seen", "reported_status", "health", "evidence_key")
    return dict(zip(keys, r, strict=False))


def site_problem(row: dict[str, Any], now: datetime, day: str | None) -> dict[str, Any]:
    """Fila de «tiendas con problemas» a partir del latido."""
    health = row.get("health") if isinstance(row.get("health"), dict) else None
    last_seen = row.get("last_seen")
    age = (now - last_seen).total_seconds() if last_seen else None
    out: dict[str, Any] = {"site_id": row["site_id"], "name": row["name"], "code": row["code"],
                           "timezone": row["timezone"], "last_seen": last_seen, "age_s": age,
                           "status": "nodata", "report_date": None, "score_min": None, "cameras_critical": 0,
                           "cameras_warning": 0, "clock_worst_s": None, "forecast_days": None, "problems": [],
                           "evidence_key_id": None}
    ek = row.get("evidence_key")
    if isinstance(ek, dict) and isinstance(ek.get("key_id"), str):
        # key_id de la clave de firma de evidencias: con él se comprueba que un paquete viene de esta tienda
        out["evidence_key_id"] = ek["key_id"][:64]
    if last_seen is None:
        out["problems"] = ["La tienda nunca ha enviado latido"]
        return out
    if health is not None:
        for k in ("report_date", "score_min", "cameras_critical", "cameras_warning", "clock_worst_s", "forecast_days"):
            out[k] = health.get(k, out[k])
        out["problems"] = [str(p)[:200] for p in (health.get("problems") or [])][:10]
        st = str(health.get("status") or "nodata")
        out["status"] = st if st in SEVERITY_ORDER else "nodata"
    else:
        out["problems"] = ["La tienda no envía el informe de salud (versión anterior a la 2.0)"]
    if day and out["report_date"] and out["report_date"] != day:
        out["status"], out["problems"] = "nodata", [f"Sin datos del {day}: el último informe es del {out['report_date']}"]
    if age is not None and age > STALE_AFTER_S:
        out["status"] = "critical"
        out["problems"] = [f"Sin latido desde hace {int(age // 60)} min: el PC de la tienda puede estar apagado o "
                           "sin red"] + out["problems"]
    return out


def sort_problems(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r["status"], 9), -(r["cameras_critical"] or 0),
                                       -(r["cameras_warning"] or 0), r["name"]))


def problems_csv(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["tienda", "codigo", "estado", "fecha_informe", "puntuacion_min", "camaras_graves", "camaras_aviso",
                "peor_desfase_s", "dias_grabacion_previstos", "problemas"])
    label = {"critical": "grave", "warning": "aviso", "ok": "correcto", "nodata": "sin datos"}
    for r in rows:
        w.writerow([text_cell(r["name"]), text_cell(r["code"]), label.get(r["status"], r["status"]),
                    text_cell(r["report_date"] or ""),
                    "" if r["score_min"] is None else r["score_min"], r["cameras_critical"], r["cameras_warning"],
                    "" if r["clock_worst_s"] is None else str(r["clock_worst_s"]).replace(".", ","),
                    "" if r["forecast_days"] is None else str(r["forecast_days"]).replace(".", ","),
                    text_cell(" | ".join(r["problems"]))])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _json(data: Any) -> Response:
    return Response(pydantic_core.to_json(data), media_type="application/json")


def _check_day(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationFailed("Fecha no válida (usa AAAA-MM-DD)") from exc


def build_router(deps: CentralDeps) -> APIRouter:
    router = APIRouter(tags=["ops"])
    Sess = Annotated[Any, Depends(deps.session)]
    Conn = Annotated[Any, Depends(deps.conn)]

    async def _rows(c: Any, site_id: str | None = None) -> list[dict[str, Any]]:
        cur = await c.execute(_SQL, {"site_id": site_id})
        return [_row_dict(r) for r in await cur.fetchall()]

    @router.get("/ops", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(WEB_DIR / "ops.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    @router.get("/api/ops/problems")
    async def problems(_: Sess, c: Conn, date: str | None = None) -> Response:
        day = _check_day(date)
        now = deps.now()
        rows = sort_problems([site_problem(r, now, day) for r in await _rows(c)])
        return _json({"date": day, "generated_at": now, "sites": rows,
                              "summary": {k: sum(1 for r in rows if r["status"] == k) for k in SEVERITY_ORDER}})

    @router.get("/api/ops/problems.csv")
    async def problems_csv_route(_: Sess, c: Conn, date: str | None = None) -> Response:
        day = _check_day(date)
        now = deps.now()
        rows = sort_problems([site_problem(r, now, day) for r in await _rows(c)])
        name = f"tiendas_con_problemas_{day or now.date().isoformat()}.csv"
        return Response(problems_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @router.get("/api/ops/sites/{site_id}/health")
    async def site_health(site_id: str, _: Sess, c: Conn, date: str | None = None) -> Response:
        if not is_valid_id(site_id):
            raise NotFoundError("La sede no existe")
        rows = await _rows(c, site_id)
        if not rows:
            raise NotFoundError("La sede no existe")
        r = rows[0]
        return _json({**site_problem(r, deps.now(), _check_day(date)), "health": r.get("health"),
                              "evidence_key": r.get("evidence_key")})

    async def _counts(c: Any, site_ids: list[str], from_: str | None, to: str | None, bucket: Bucket) \
            -> tuple[list[CountRow], datetime, datetime, str]:
        cur = await c.execute("SELECT site_id, name, code, timezone FROM sites WHERE site_id = ANY(%s) "
                              "ORDER BY site_id", (site_ids,))
        sites = [_row_dict(r) if isinstance(r, dict) else dict(zip(("site_id", "name", "code", "timezone"), r,
                                                                     strict=False)) for r in await cur.fetchall()]
        if len(sites) != len(set(site_ids)):
            raise NotFoundError("Alguna sede no existe")
        tz = sites[0]["timezone"]
        start, end = parse_range(from_, to, tz, bucket)
        out: list[CountRow] = []
        for s in sites:
            label = f"{s['name']} ({s['code']})" if s.get("code") else s["name"]
            out += await fetch_rows(c, s["site_id"], label, s["timezone"], start, end, bucket)
        return out, start, end, tz

    @router.get("/api/sites/{site_id}/counts.csv")
    async def site_counts_csv(site_id: str, _: Sess, c: Conn,
                              from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None,
                              bucket: Literal["hour", "day"] = "hour") -> Response:
        if not is_valid_id(site_id):
            raise NotFoundError("La sede no existe")
        rows, start, end, tz = await _counts(c, [site_id], from_, to, bucket)
        return Response(render_csv(rows, bucket), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{filename(site_id, start, end, tz)}"',
            "Cache-Control": "no-store"})

    @router.get("/api/counts.csv")
    async def counts_csv(_: Sess, c: Conn, sites: str, from_: Annotated[str | None, Query(alias="from")] = None,
                         to: str | None = None, bucket: Literal["hour", "day"] = "day") -> Response:
        ids = [s.strip() for s in sites.split(",") if s.strip()]
        if not ids or len(ids) > 200 or not all(is_valid_id(s) for s in ids):
            raise ValidationFailed("Indica entre 1 y 200 sedes separadas por comas")
        rows, start, end, tz = await _counts(c, ids, from_, to, bucket)
        return Response(render_csv(rows, bucket), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{filename("sedes", start, end, tz)}"',
            "Cache-Control": "no-store"})

    return router
