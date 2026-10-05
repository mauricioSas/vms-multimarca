"""Marcadores con bloqueo de retención y exportación de evidencias firmada (CONTRATO §18.6 y §18.7).

Dueño: B6.

| Método y ruta | Rol |
|---|---|
| `GET /api/bookmarks?camera_id=&from=&to=` | O (permiso `playback` sobre la cámara) |
| `POST /api/bookmarks` | O con permiso `bookmark`; `protect` exige A o permiso `export` (operador: ≤ 90 días y ≤ 48 h protegidas por cámara) |
| `PATCH /api/bookmarks/{id}` · `DELETE /api/bookmarks/{id}` | A (se audita) |
| `POST /api/evidence/exports` (motivo obligatorio) → 202 | O con permiso `export` en cada cámara |
| `GET /api/evidence/exports?limit=&offset=` · `GET /api/evidence/exports/{id}` | O (los suyos) o A (todos) |
| `GET /api/evidence/exports/{id}/download` (ZIP en streaming) | quien la creó (con permiso `export` vigente en sus cámaras) o A |
| `DELETE /api/evidence/exports/{id}` | A |
| `GET /api/evidence/settings` · `PUT /api/evidence/settings` (`export_retention_days` 1-365) | A |

Los paquetes caducan solos a los `export_retention_days` días (30 por defecto): el mantenimiento horario
borra el ZIP y su registro y lo anota en audit.log (`evidence_export_expired`).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import FileResponse, Response

from vms.api.events import publish_bookmark
from vms.core.audit import audit
from vms.core.errors import ForbiddenError, NotFoundError, ValidationFailed
from vms.ops.models import Bookmark, BookmarkCreate, BookmarkUpdate, EvidenceExport, EvidenceExportRequest

from ..deps import Principal, get_state, require_admin, require_operator
from ..errors import json_response
from ..permissions import camera_allowed, ensure_camera_access
from ..security import client_ip
from ..state import AppState
from .health import get_ops

if TYPE_CHECKING:
    from vms.ops.service import OpsService

log = logging.getLogger("vms.api.evidence")
router = APIRouter(prefix="/api", tags=["evidence"])


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _get_bookmark(ops: "OpsService", bookmark_id: str) -> Bookmark:
    bm = ops.store.get_bookmark(bookmark_id)
    if bm is None:
        raise NotFoundError("El marcador no existe")
    return bm


# --------------------------------------------------------------------------- marcadores
@router.get("/bookmarks")
async def list_bookmarks(camera_id: str | None = None,
                         from_: Annotated[datetime | None, Query(alias="from")] = None, to: datetime | None = None,
                         p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                         ops: "OpsService" = Depends(get_ops)) -> Response:
    if camera_id is not None:
        ensure_camera_access(state, p, camera_id, "playback")
    items = await asyncio.to_thread(ops.store.bookmarks, camera_id, _utc(from_), _utc(to))
    return json_response([b for b in items if camera_allowed(state, p, b.camera_id, "playback")])


@router.post("/bookmarks")
async def create_bookmark(body: BookmarkCreate, request: Request, p: Principal = Depends(require_operator),
                          state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    ensure_camera_access(state, p, body.camera_id, "bookmark")
    if state.config().camera(body.camera_id) is None:
        raise NotFoundError("La cámara no existe")
    if body.protect and p.role != "admin" and not camera_allowed(state, p, body.camera_id, "export"):
        raise ForbiddenError("Proteger un tramo exige permiso de exportación en esa cámara")
    bm = await asyncio.to_thread(lambda: ops.bookmarks.create(body, p.username, is_admin=p.role == "admin"))
    audit("bookmark_create", user=p.username, ip=client_ip(request), bookmark_id=bm.id, camera_id=bm.camera_id,
          start=bm.start.isoformat(), end=bm.end.isoformat() if bm.end else None, protected=bm.protected,
          reason=bm.protect_reason or None, case_ref=bm.case_ref or None,
          until=bm.protect_until.isoformat() if bm.protect_until else None)
    publish_bookmark(state.bus, {"action": "created", "bookmark_id": bm.id, "camera_id": bm.camera_id})
    return json_response(bm, 201)


@router.patch("/bookmarks/{bookmark_id}")
async def update_bookmark(bookmark_id: str, body: BookmarkUpdate, request: Request,
                          p: Principal = Depends(require_admin), state: AppState = Depends(get_state),
                          ops: "OpsService" = Depends(get_ops)) -> Response:
    bm = _get_bookmark(ops, bookmark_id)
    changes = {k: getattr(body, k) for k in ("note", "case_ref") if getattr(body, k) is not None}
    if changes:
        bm = bm.model_copy(update=changes)
        await asyncio.to_thread(ops.store.save_bookmark, bm)
    action = "updated"
    if body.protect is True:
        reason = body.protect_reason if body.protect_reason is not None else bm.protect_reason
        bm = await asyncio.to_thread(ops.bookmarks.protect, bm, reason or "", body.protect_days or 90, p.username)
        action = "protect"
    elif body.protect is False and bm.protected:
        reason = (body.protect_reason or "").strip()
        if len(reason) < 3:
            raise ValidationFailed("Indica el motivo para dejar de proteger el tramo",
                                   details={"fields": [{"loc": ["protect_reason"], "msg": "Campo obligatorio"}]})
        bm = await asyncio.to_thread(ops.bookmarks.release, bm, reason)
        action = "unprotect"
    audit(f"bookmark_{action}", user=p.username, ip=client_ip(request), bookmark_id=bm.id, camera_id=bm.camera_id,
          reason=body.protect_reason, fields=sorted(changes))
    publish_bookmark(state.bus, {"action": "updated", "bookmark_id": bm.id, "camera_id": bm.camera_id})
    return json_response(bm)


@router.delete("/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: str, request: Request, p: Principal = Depends(require_admin),
                          state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    bm = _get_bookmark(ops, bookmark_id)
    await asyncio.to_thread(ops.bookmarks.delete, bm)
    audit("bookmark_delete", user=p.username, ip=client_ip(request), bookmark_id=bm.id, camera_id=bm.camera_id,
          was_protected=bm.protected)
    publish_bookmark(state.bus, {"action": "deleted", "bookmark_id": bm.id, "camera_id": bm.camera_id})
    return Response(status_code=204)


# --------------------------------------------------------------------------- exportaciones
@router.post("/evidence/exports")
async def create_export(body: EvidenceExportRequest, request: Request, p: Principal = Depends(require_operator),
                        state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    for cid in body.camera_ids:
        ensure_camera_access(state, p, cid, "export")
    if _utc(body.end) <= _utc(body.start):  # type: ignore[operator]
        raise ValidationFailed("El final debe ser posterior al inicio",
                               details={"fields": [{"loc": ["end"], "msg": "Debe ser posterior al inicio"}]})
    if not body.reason.strip():
        raise ValidationFailed("El motivo es obligatorio", details={"fields": [{"loc": ["reason"],
                                                                               "msg": "Campo obligatorio"}]})
    exp = ops.start_export(body, p.username, client_ip(request))
    return json_response(exp, 202)


@router.get("/evidence/exports")
async def list_exports(limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
                       p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                       ops: "OpsService" = Depends(get_ops)) -> Response:
    """Paginado (de la más reciente a la más antigua). Pide `limit + 1` para saber si hay más."""
    who = None if p.role == "admin" else p.username
    items = await asyncio.to_thread(ops.store.exports, limit, offset, who)
    return json_response([e for e in items if _export_allowed(state, p, e)])


def _export_allowed(state: AppState, p: Principal, exp: EvidenceExport) -> bool:
    """Un operador solo ve y descarga SUS exportaciones y solo mientras conserve el permiso `export` en todas sus
    cámaras: quitarle el permiso o una cámara del ámbito vale en el acto también para lo ya exportado."""
    if p.role == "admin":
        return True
    if exp.created_by != p.username:
        return False
    return all(camera_allowed(state, p, cid, "export") for cid in exp.request.camera_ids)


def _visible_export(state: AppState, ops: "OpsService", export_id: str, p: Principal) -> EvidenceExport:
    exp = ops.store.get_export(export_id)
    if exp is None or not _export_allowed(state, p, exp):
        raise NotFoundError("La exportación no existe")
    return exp


@router.get("/evidence/exports/{export_id}")
async def get_export(export_id: str, p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                     ops: "OpsService" = Depends(get_ops)) -> Response:
    return json_response(_visible_export(state, ops, export_id, p))


@router.get("/evidence/exports/{export_id}/download")
async def download_export(export_id: str, request: Request, p: Principal = Depends(require_operator),
                          state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    exp = _visible_export(state, ops, export_id, p)
    path = ops.builder.zip_path(exp.export_id)
    if exp.state != "done" or not path.is_file():
        raise NotFoundError("El paquete todavía no está listo o se borró")
    audit("evidence_download", user=p.username, ip=client_ip(request), export_id=exp.export_id,
          sha256_manifest=exp.sha256_manifest)
    return FileResponse(path, media_type="application/zip", filename=f"{exp.export_id}.zip",
                        headers={"Cache-Control": "no-store"})


@router.delete("/evidence/exports/{export_id}")
async def delete_export(export_id: str, request: Request, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    exp = _visible_export(state, ops, export_id, p)
    await asyncio.to_thread(ops.delete_export, exp.export_id)
    audit("evidence_export_delete", user=p.username, ip=client_ip(request), export_id=exp.export_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- ajustes de evidencias
class EvidenceSettings(BaseModel):
    export_retention_days: int = Field(ge=1, le=365, description="Días que se guardan los paquetes exportados")


@router.get("/evidence/settings")
async def get_evidence_settings(_: Principal = Depends(require_admin), ops: "OpsService" = Depends(get_ops)) -> Response:
    return json_response({"export_retention_days": ops.export_retention_days()})


@router.put("/evidence/settings")
async def put_evidence_settings(body: EvidenceSettings, request: Request, p: Principal = Depends(require_admin),
                                ops: "OpsService" = Depends(get_ops)) -> Response:
    before = ops.export_retention_days()
    ops.set_export_retention_days(body.export_retention_days)
    audit("evidence_settings", user=p.username, ip=client_ip(request),
          export_retention_days=body.export_retention_days, before=before)
    return json_response({"export_retention_days": body.export_retention_days})
