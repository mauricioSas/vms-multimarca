"""Línea de tiempo con eventos (CONTRATO §18.13). Dueño: B6.

`GET /api/timeline/{camera_id}?start=&end=&layers=recording_gap,bookmark,health,clock,analytics_alert,protected`
(O, permiso `playback`) → `[TimelineEvent]`. Las capas se pintan sobre la línea de tiempo de `playback.html`.
Los eventos del NVR (`nvr_event`) llegan en la v2.1.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from starlette.responses import Response

from vms.core.errors import NotFoundError, ValidationFailed

from ..deps import Principal, get_state, require_operator
from ..errors import json_response
from ..permissions import ensure_camera_access
from ..state import AppState
from .health import get_ops

if TYPE_CHECKING:
    from vms.ops.service import OpsService

router = APIRouter(prefix="/api/timeline", tags=["timeline"])

ALL_LAYERS = ("recording_gap", "bookmark", "health", "clock", "analytics_alert", "protected")
MAX_SPAN = timedelta(days=7)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@router.get("/{camera_id}")
async def timeline(camera_id: str, start: datetime, end: datetime, layers: str = ",".join(ALL_LAYERS),
                   p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                   ops: "OpsService" = Depends(get_ops)) -> Response:
    ensure_camera_access(state, p, camera_id, "playback")
    if state.config().camera(camera_id) is None:
        raise NotFoundError("La cámara no existe")
    s, e = _utc(start), _utc(end)
    if e <= s:
        raise ValidationFailed("El final debe ser posterior al inicio")
    if e - s > MAX_SPAN:
        raise ValidationFailed("Como mucho 7 días por consulta")
    wanted = {x.strip() for x in layers.split(",") if x.strip()}
    unknown = wanted - set(ALL_LAYERS) - {"nvr_event"}
    if unknown:
        raise ValidationFailed(f"Capas desconocidas: {', '.join(sorted(unknown))}")
    return json_response(await ops.timeline(camera_id, s, e, wanted))
