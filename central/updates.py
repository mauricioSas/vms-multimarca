"""Panel central: versiones por sede, canal, retener, ventana, comprobar y volver atrás (CONTRATO §15.7). Dueño: B4.

Rutas (registradas por `central/extensions.py`):
- `GET  /api/updates/sites` (cualquier usuario): versión instalada, estado de la actualización y lo pedido.
- `PUT  /api/updates/sites/{id}` (admin): `{"channel"?, "hold"?, "window"?}`.
- `POST /api/updates/sites/{id}/rollback` (admin): `{"to": "2.0.0" | null}` (null = la versión anterior).
- `POST /api/updates/sites/{id}/check` (admin): comprobar en el próximo latido.
- `GET  /updates`: página «Versiones».

El panel **no firma nada** ni tiene credenciales de Cloudflare: solo cambia el canal de una sede entre los
canales firmados, retiene sus actualizaciones o pide su vuelta atrás. Lo pedido viaja en la respuesta del
latido (`directive_for`) y lo aplica `VMSUpdater` en la tienda (CONTRATO §15.6).
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
REPORTED = ("installed", "state", "last_result", "message_es", "available", "last_check", "channel", "hold",
            "metadata_expires", "clock_skew_s", "reboot_pending", "updater_version")


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
       COALESCE(v.channel, 'stable') AS channel, COALESCE(v.hold, false) AS hold,
       COALESCE(v.window_local, '01:00-03:00') AS window_local, v.rollback_to, v.rollback_requested_at,
       v.check_requested_at, v.updated_at, v.updated_by
FROM sites s
LEFT JOIN site_heartbeats h ON h.site_id = s.site_id
LEFT JOIN site_versions v ON v.site_id = s.site_id
WHERE (%(site_id)s::text IS NULL OR s.site_id = %(site_id)s::text)
ORDER BY s.active DESC, s.name
"""

_UPSERT_SQL = """
INSERT INTO site_versions (site_id, channel, hold, window_local, updated_at, updated_by)
VALUES (%(site_id)s, COALESCE(%(channel)s, 'stable'), COALESCE(%(hold)s, false), COALESCE(%(window)s, '01:00-03:00'),
        %(now)s, %(by)s)
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
    rep = row.get("reported") if isinstance(row.get("reported"), dict) else {}
    installed = rep.get("installed") or row.get("installed") or row.get("heartbeat_version") or ""
    rollback_to = row.get("rollback_to")
    req_at = row.get("rollback_requested_at")
    if rollback_to and isinstance(req_at, datetime) and now - req_at > ROLLBACK_TTL:
        rollback_to = None
    wanted_channel = row.get("channel") or "stable"
    reported_channel = rep.get("channel")
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
        "reported_hold": rep.get("hold"),
        "channel": wanted_channel, "hold": bool(row.get("hold")), "window": row.get("window_local") or "01:00-03:00",
        "rollback_to": rollback_to, "rollback_requested_at": _iso(req_at) if rollback_to else None,
        "check_requested_at": _iso(row.get("check_requested_at")),
        "pending": (reported_channel is not None and reported_channel != wanted_channel)
                   or (rep.get("hold") is not None and bool(rep.get("hold")) != bool(row.get("hold"))),
        "updated_at": _iso(row.get("updated_at")), "updated_by": row.get("updated_by") or "",
    }


async def list_site_versions(conn: AsyncConnection[Any], now: datetime, site_id: str | None = None) -> list[dict[str, Any]]:
    return [site_out(r, now) for r in await _fetch(conn, _LIST_SQL, {"site_id": site_id})]


async def directive_for(conn: AsyncConnection[Any], site_id: str, now: datetime,
                        reported: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Lo que el panel pide a una sede, para la respuesta del latido: `{"update": {...}}` o None.

    Si llega `reported` (= `payload.update` del latido), se copia en `site_versions`. Pensado para que lo
    llame el manejador de `POST /api/heartbeat` (petición al arquitecto, CONTRATO §12)."""
    if reported:
        clean = {k: reported.get(k) for k in REPORTED if k in reported}
        await conn.execute(
            """INSERT INTO site_versions (site_id, installed, update_state, last_result, message_es, available,
                                          reported_at)
               VALUES (%(site_id)s, %(installed)s, %(state)s, %(last_result)s, %(message_es)s, %(available)s, %(now)s)
               ON CONFLICT (site_id) DO UPDATE SET installed = EXCLUDED.installed,
                   update_state = EXCLUDED.update_state, last_result = EXCLUDED.last_result,
                   message_es = EXCLUDED.message_es, available = EXCLUDED.available, reported_at = EXCLUDED.reported_at""",
            {"site_id": site_id, "installed": str(clean.get("installed") or "")[:40],
             "state": str(clean.get("state") or "unknown")[:40], "last_result": str(clean.get("last_result") or "none")[:40],
             "message_es": str(clean.get("message_es") or "")[:500],
             "available": (str(clean["available"])[:40] if clean.get("available") else None), "now": now})
    rows = await _fetch(conn, "SELECT * FROM site_versions WHERE site_id = %(site_id)s", {"site_id": site_id})
    if not rows:
        return None
    r = rows[0]
    out: dict[str, Any] = {"channel": r["channel"], "hold": r["hold"], "window": r["window_local"],
                           "check": False, "rollback_to": None, "received": _iso(r["updated_at"])}
    if r.get("check_requested_at") and (not r.get("reported_at") or r["check_requested_at"] > r["reported_at"]
                                        or now - r["check_requested_at"] < timedelta(minutes=10)):
        out["check"] = True
        out["received"] = _iso(r["check_requested_at"])
    req = r.get("rollback_requested_at")
    if r.get("rollback_to") and isinstance(req, datetime) and now - req <= ROLLBACK_TTL:
        out["rollback_to"] = r["rollback_to"]
        out["received"] = _iso(req)
    return {"update": out}


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

    @router.get("/updates", include_in_schema=False, response_model=None)
    async def page(request: Request) -> Response:
        try:
            deps.session(request)
        except Exception:  # noqa: BLE001 - sin sesión: a la página de inicio de sesión
            return RedirectResponse("/login?next=/updates", status_code=303)
        return FileResponse(WEB_DIR / "updates.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    return router
