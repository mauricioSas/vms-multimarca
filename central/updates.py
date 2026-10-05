"""Panel central: versiones por sede, canal, retener, ventana, comprobar y volver atrás (CONTRATO §15.7). Dueño: B4.

Rutas (registradas por `central/extensions.py`):
- `GET  /api/updates/sites` (cualquier usuario): versión instalada, estado de la actualización y lo pedido.
- `PUT  /api/updates/sites/{id}` (admin): `{"channel"?, "hold"?, "window"?}`.
- `POST /api/updates/sites/{id}/rollback` (admin): `{"to": "2.0.0" | null}` (null = la versión anterior).
- `POST /api/updates/sites/{id}/check` (admin): comprobar en el próximo latido.
- `POST /api/updates/sites/{id}/unskip` (admin): volver a permitir la versión que se dejó tras una vuelta atrás.
- `GET  /updates`: página «Versiones».

El panel **no firma nada** ni tiene credenciales de Cloudflare: solo cambia el canal de una sede entre los
canales firmados, retiene sus actualizaciones o pide su vuelta atrás. Lo pedido viaja en la respuesta del
latido (`directive_for`) y lo aplica `VMSUpdater` en la tienda (CONTRATO §15.6).

Lo que **informa** la sede (`reported_*`) y lo que **pide** el panel (`channel`, `hold`, `window_local`) van
en columnas distintas: una columna de petición a NULL significa «el panel no pide nada» y la tienda sigue con
lo que configuró el instalador. La directiva solo lleva lo que el panel fijó de forma explícita.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, Field, field_validator

from vms.core.errors import NotFoundError, ValidationFailed

from .extensions import CentralDeps

WEB_DIR = Path(__file__).resolve().parent / "web"
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
WINDOW_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$")
ROLLBACK_TTL = timedelta(hours=24)        # una petición de vuelta atrás no entregada caduca a las 24 h
DEFAULT_CHANNEL = "stable"
DEFAULT_WINDOW = "01:00-03:00"
REPORTED = ("installed", "state", "last_result", "message_es", "available", "last_check", "channel", "hold",
            "window", "skipped", "metadata_expires", "clock_skew_s", "reboot_pending", "updater_version")
_CHANNEL_OK = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


class SiteUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str | None = Field(None, pattern=CHANNEL_RE.pattern)
    hold: bool | None = None
    window: str | None = Field(None, pattern=WINDOW_RE.pattern)


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str | None = None

    @field_validator("to")
    @classmethod
    def _to(cls, v: str | None) -> str | None:
        if v is not None and not VERSION_RE.match(v):
            raise ValueError("versión no válida (X.Y.Z)")
        return v


_LIST_SQL = """
SELECT s.site_id, s.name, s.code, s.active, h.last_seen, h.version AS heartbeat_version,
       h.payload -> 'update' AS reported,
       v.installed, v.update_state, v.last_result, v.message_es, v.available, v.reported_at,
       v.reported_channel, v.reported_hold, v.reported_window, v.skipped,
       v.channel, v.hold, v.window_local, v.rollback_to, v.rollback_requested_at,
       v.check_requested_at, v.unskip_requested_at, v.updated_at, v.updated_by
FROM sites s
LEFT JOIN site_heartbeats h ON h.site_id = s.site_id
LEFT JOIN site_versions v ON v.site_id = s.site_id
WHERE (%(site_id)s::text IS NULL OR s.site_id = %(site_id)s::text)
ORDER BY s.active DESC, s.name
"""

_UPSERT_SQL = """
INSERT INTO site_versions (site_id, channel, hold, window_local, updated_at, updated_by)
VALUES (%(site_id)s, %(channel)s, %(hold)s, %(window)s, %(now)s, %(by)s)
ON CONFLICT (site_id) DO UPDATE SET
    channel = COALESCE(%(channel)s, site_versions.channel),
    hold = COALESCE(%(hold)s, site_versions.hold),
    window_local = COALESCE(%(window)s, site_versions.window_local),
    updated_at = %(now)s, updated_by = %(by)s
"""


async def _fetch(conn: AsyncConnection[Any], sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)  # type: ignore[arg-type]
        rows = await cur.fetchall()
    return [dict(r) for r in rows]  # type: ignore[call-overload]


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def site_out(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    raw_rep = row.get("reported")
    rep: dict[str, Any] = raw_rep if isinstance(raw_rep, dict) else {}
    installed = rep.get("installed") or row.get("installed") or row.get("heartbeat_version") or ""
    rollback_to = row.get("rollback_to")
    req_at = row.get("rollback_requested_at")
    if rollback_to and isinstance(req_at, datetime) and now - req_at > ROLLBACK_TTL:
        rollback_to = None
    reported_channel = rep.get("channel") if rep.get("channel") is not None else row.get("reported_channel")
    reported_hold = rep.get("hold") if rep.get("hold") is not None else row.get("reported_hold")
    reported_window = rep.get("window") or row.get("reported_window")
    req_channel, req_hold, req_window = row.get("channel"), row.get("hold"), row.get("window_local")
    # Lo que se ve: lo pedido por el panel si pidió algo; si no, lo que informa la tienda.
    channel = req_channel or reported_channel or DEFAULT_CHANNEL
    hold = bool(req_hold) if req_hold is not None else bool(reported_hold)
    window = req_window or reported_window or DEFAULT_WINDOW
    raw_skipped = rep.get("skipped")
    skipped: list[Any] = raw_skipped if isinstance(raw_skipped, list) else list(row.get("skipped") or [])
    return {
        "site_id": row["site_id"], "name": row["name"], "code": row.get("code") or "", "active": row.get("active", True),
        "last_seen": _iso(row.get("last_seen")),
        "installed": installed,
        "state": rep.get("state") or row.get("update_state") or "unknown",
        "last_result": rep.get("last_result") or row.get("last_result") or "none",
        "message_es": rep.get("message_es") or row.get("message_es") or "",
        "available": rep.get("available", row.get("available")),
        "last_check": rep.get("last_check"),
        "reboot_pending": bool(rep.get("reboot_pending", False)),
        "clock_skew_s": rep.get("clock_skew_s"),
        "reported_channel": reported_channel,
        "reported_hold": reported_hold,
        "channel": channel, "hold": hold, "window": window,
        "requested": {"channel": req_channel, "hold": req_hold, "window": req_window},
        "skipped": [str(v) for v in skipped],
        "rollback_to": rollback_to, "rollback_requested_at": _iso(req_at) if rollback_to else None,
        "check_requested_at": _iso(row.get("check_requested_at")),
        "unskip_requested_at": _iso(row.get("unskip_requested_at")),
        "pending": (req_channel is not None and reported_channel is not None and reported_channel != req_channel)
                   or (req_hold is not None and reported_hold is not None and bool(reported_hold) != bool(req_hold))
                   or (req_window is not None and reported_window is not None and reported_window != req_window),
        "updated_at": _iso(row.get("updated_at")), "updated_by": row.get("updated_by") or "",
    }


async def list_site_versions(conn: AsyncConnection[Any], now: datetime, site_id: str | None = None) -> list[dict[str, Any]]:
    return [site_out(r, now) for r in await _fetch(conn, _LIST_SQL, {"site_id": site_id})]


_REPORT_SQL = """INSERT INTO site_versions (site_id, installed, update_state, last_result, message_es, available,
                                          reported_at, reported_channel, reported_hold, reported_window, skipped)
               VALUES (%(site_id)s, %(installed)s, %(state)s, %(last_result)s, %(message_es)s, %(available)s, %(now)s,
                       %(r_channel)s, %(r_hold)s, %(r_window)s, %(skipped)s)
               ON CONFLICT (site_id) DO UPDATE SET installed = EXCLUDED.installed,
                   update_state = EXCLUDED.update_state, last_result = EXCLUDED.last_result,
                   message_es = EXCLUDED.message_es, available = EXCLUDED.available, reported_at = EXCLUDED.reported_at,
                   reported_channel = EXCLUDED.reported_channel, reported_hold = EXCLUDED.reported_hold,
                   reported_window = EXCLUDED.reported_window, skipped = EXCLUDED.skipped"""
_ROW_SQL = "SELECT * FROM site_versions WHERE site_id = %(site_id)s"
# Columnas que una sede puede escribir con su rol de PostgreSQL (latido directo): solo lo que INFORMA.
REPORTED_COLUMNS = ("site_id", "installed", "update_state", "last_result", "message_es", "available", "reported_at",
                    "reported_channel", "reported_hold", "reported_window", "skipped")


def _report_params(site_id: str, now: datetime, reported: dict[str, Any]) -> dict[str, Any]:
    clean = {k: reported.get(k) for k in REPORTED if k in reported}
    ch = clean.get("channel")
    win = clean.get("window")
    skipped = clean.get("skipped")
    return {"site_id": site_id, "installed": str(clean.get("installed") or "")[:40],
            "state": str(clean.get("state") or "unknown")[:40], "last_result": str(clean.get("last_result") or "none")[:40],
            "message_es": str(clean.get("message_es") or "")[:500],
            "available": (str(clean["available"])[:40] if clean.get("available") else None), "now": now,
            "r_channel": ch if isinstance(ch, str) and _CHANNEL_OK.match(ch) else None,
            "r_hold": clean["hold"] if isinstance(clean.get("hold"), bool) else None,
            "r_window": win if isinstance(win, str) and WINDOW_RE.match(win) else None,
            "skipped": [str(v)[:40] for v in skipped[:20] if VERSION_RE.match(str(v))]
                       if isinstance(skipped, list) else []}


def directive_from_row(r: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """`{"update": {...}}` a partir de la fila de `site_versions` (None si el panel no pide nada)."""
    if not r:
        return None
    out: dict[str, Any] = {}
    if r.get("channel") is not None:
        out["channel"] = r["channel"]
    if r.get("hold") is not None:
        out["hold"] = r["hold"]
    if r.get("window_local") is not None:
        out["window"] = r["window_local"]
    if out:
        out["received"] = _iso(r["updated_at"])
    chk = r.get("check_requested_at")
    if chk and (not r.get("reported_at") or chk > r["reported_at"] or now - chk < timedelta(minutes=10)):
        out["check"] = True
        out["received"] = _iso(chk)
    req = r.get("rollback_requested_at")
    if r.get("rollback_to") and isinstance(req, datetime) and now - req <= ROLLBACK_TTL:
        out["rollback_to"] = r["rollback_to"]
        out["received"] = _iso(req)
    unskip = r.get("unskip_requested_at")
    if isinstance(unskip, datetime) and now - unskip <= ROLLBACK_TTL:
        out["unskip_at"] = _iso(unskip)
    if not out:
        return None
    out.setdefault("check", False)
    out.setdefault("rollback_to", None)
    return {"update": out}


async def directive_for(conn: AsyncConnection[Any], site_id: str, now: datetime,
                        reported: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Lo que el panel pide a una sede, para la respuesta del latido: `{"update": {...}}` o None.

    Solo lleva lo que el panel fijó de forma explícita: `channel`, `hold` y `window` aparecen únicamente si
    un administrador los cambió; si no, la tienda sigue con lo suyo (lo que puso el instalador). Si no hay
    nada pedido, devuelve None.

    Si llega `reported` (= `payload.update` del latido), se copia en las columnas `reported_*` y nunca en las
    de petición. La llaman el manejador de `POST /api/heartbeat` (agente HTTP) y, con `directive_for_sync`, el
    latido directo a PostgreSQL: los dos entregan lo mismo a la tienda."""
    if reported:
        await conn.execute(_REPORT_SQL, _report_params(site_id, now, reported))
    rows = await _fetch(conn, _ROW_SQL, {"site_id": site_id})
    return directive_from_row(rows[0] if rows else None, now)


def directive_for_sync(conn: Any, site_id: str, now: datetime,
                       reported: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """`directive_for` con una conexión síncrona de psycopg (latido directo desde el backend, en un hilo)."""
    from psycopg.rows import dict_row

    if reported:
        conn.execute(_REPORT_SQL, _report_params(site_id, now, reported))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_ROW_SQL, {"site_id": site_id})
        row = cur.fetchone()
    return directive_from_row(dict(row) if row else None, now)


def build_router(deps: CentralDeps) -> APIRouter:
    router = APIRouter(tags=["updates"])
    # Sin alias Annotated locales: con `from __future__ import annotations` FastAPI no los resolvería.
    session_dep = Depends(deps.session)
    admin_dep = Depends(deps.admin)
    conn_dep = Depends(deps.conn)

    async def _one(c: AsyncConnection[Any], site_id: str) -> dict[str, Any]:
        rows = await list_site_versions(c, deps.now(), site_id)
        if not rows:
            raise NotFoundError("Sede no encontrada")
        return rows[0]

    @router.get("/api/updates/sites")
    async def sites(_: Any = session_dep, c: Any = conn_dep) -> JSONResponse:
        return JSONResponse(await list_site_versions(c, deps.now()))

    @router.put("/api/updates/sites/{site_id}")
    async def put_site(site_id: str, body: SiteUpdateIn = Body(...), s: Any = admin_dep, c: Any = conn_dep) -> JSONResponse:
        await _one(c, site_id)
        if body.channel is None and body.hold is None and body.window is None:
            raise ValidationFailed("No hay nada que cambiar")
        await c.execute(_UPSERT_SQL, {"site_id": site_id, "channel": body.channel, "hold": body.hold,  # type: ignore[arg-type]
                                      "window": body.window, "now": deps.now(), "by": s.username})
        return JSONResponse(await _one(c, site_id))

    @router.post("/api/updates/sites/{site_id}/rollback")
    async def rollback(site_id: str, body: RollbackIn = Body(default=RollbackIn()), s: Any = admin_dep,
                       c: Any = conn_dep) -> JSONResponse:
        await _one(c, site_id)
        await c.execute(_UPSERT_SQL, {"site_id": site_id, "channel": None, "hold": None, "window": None,  # type: ignore[arg-type]
                                      "now": deps.now(), "by": s.username})
        await c.execute("""UPDATE site_versions SET rollback_to = %(to)s, rollback_requested_at = %(now)s,
                               updated_at = %(now)s, updated_by = %(by)s WHERE site_id = %(site_id)s""",
                        {"to": body.to or "previous", "now": deps.now(), "by": s.username, "site_id": site_id})
        return JSONResponse(await _one(c, site_id))

    @router.delete("/api/updates/sites/{site_id}/rollback")
    async def cancel_rollback(site_id: str, s: Any = admin_dep, c: Any = conn_dep) -> JSONResponse:
        await _one(c, site_id)
        await c.execute("""UPDATE site_versions SET rollback_to = NULL, rollback_requested_at = NULL,
                               updated_at = %(now)s, updated_by = %(by)s WHERE site_id = %(site_id)s""",
                        {"now": deps.now(), "by": s.username, "site_id": site_id})
        return JSONResponse(await _one(c, site_id))

    @router.post("/api/updates/sites/{site_id}/check")
    async def check(site_id: str, s: Any = admin_dep, c: Any = conn_dep) -> JSONResponse:
        await _one(c, site_id)
        await c.execute(_UPSERT_SQL, {"site_id": site_id, "channel": None, "hold": None, "window": None,  # type: ignore[arg-type]
                                      "now": deps.now(), "by": s.username})
        await c.execute("UPDATE site_versions SET check_requested_at = %(now)s WHERE site_id = %(site_id)s",
                        {"now": deps.now(), "site_id": site_id})
        return JSONResponse(await _one(c, site_id))

    @router.post("/api/updates/sites/{site_id}/unskip")
    async def unskip(site_id: str, s: Any = admin_dep, c: Any = conn_dep) -> JSONResponse:
        """Vuelve a permitir la versión que la sede dejó de lado tras una vuelta atrás manual."""
        await _one(c, site_id)
        await c.execute(_UPSERT_SQL, {"site_id": site_id, "channel": None, "hold": None, "window": None,  # type: ignore[arg-type]
                                      "now": deps.now(), "by": s.username})
        await c.execute("""UPDATE site_versions SET unskip_requested_at = %(now)s, updated_at = %(now)s,
                               updated_by = %(by)s WHERE site_id = %(site_id)s""",
                        {"now": deps.now(), "by": s.username, "site_id": site_id})
        return JSONResponse(await _one(c, site_id))

    @router.get("/updates", include_in_schema=False, response_model=None)
    async def page(request: Request) -> Response:
        try:
            deps.session(request)
        except Exception:  # noqa: BLE001 - sin sesión: a la página de inicio de sesión
            return RedirectResponse("/login?next=/updates", status_code=303)
        return FileResponse(WEB_DIR / "updates.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    return router
