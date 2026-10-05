"""Muros (disposición de cada monitor) (CONTRATO §6.5)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from vms.core.errors import NotFoundError, ValidationFailed
from vms.core.models import MAX_MONITORS, WALL_SLOTS, AppConfig, WallLayout, WallUpdate

from ..deps import Principal, get_state, require_kiosk, require_operator
from ..errors import json_response
from ..permissions import visible_camera_ids
from ..state import AppState

log = logging.getLogger("vms.api.walls")
router = APIRouter(prefix="/api/walls", tags=["walls"])


def _get_wall(cfg: AppConfig, monitor: int) -> WallLayout:
    wall = cfg.wall(monitor) if 1 <= monitor <= MAX_MONITORS else None
    if wall is None:
        raise NotFoundError(f"El monitor {monitor} no existe (1 a {MAX_MONITORS})")
    return wall


@router.get("")
async def list_walls(_: Principal = Depends(require_kiosk), state: AppState = Depends(get_state)) -> Response:
    return json_response(sorted(state.config().walls, key=lambda w: w.monitor))


@router.get("/{monitor}")
async def get_wall(monitor: int, request: Request, _: Principal = Depends(require_kiosk),
                   state: AppState = Depends(get_state)) -> Response:
    wall = _get_wall(state.config(), monitor)
    etag = f'"{state.repo.revision}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return json_response(wall, headers={"ETag": etag, "Cache-Control": "no-cache"})


@router.put("/{monitor}")
async def put_wall(monitor: int, body: WallUpdate, p: Principal = Depends(require_operator),
                   state: AppState = Depends(get_state)) -> Response:
    def mutate(cfg: AppConfig) -> WallLayout:
        wall = _get_wall(cfg, monitor)
        if body.cells is not None:
            ids = {c.id for c in cfg.cameras}
            # Una cámara que se AÑADE al muro tiene que estar en el ámbito del usuario («live», CONTRATO
            # §18.8): si no, el kiosco se la enseñaría. Fuera del ámbito = «no existe» (no se revela). Las que
            # ya estaban (puestas por un administrador) se pueden dejar o quitar.
            added = {c for c in body.cells if c is not None and c in ids and c not in wall.cells}
            allowed = visible_camera_ids(state, p, added, "live")
            bad = [{"loc": ["cells", i], "msg": "La cámara no existe"} for i, c in enumerate(body.cells)
                   if c is not None and (c not in ids or (c in added and c not in allowed))]
            if bad:
                raise ValidationFailed("Hay cámaras que no existen en el muro", details={"fields": bad})
            wall.cells = (list(body.cells) + [None] * WALL_SLOTS)[:WALL_SLOTS]
        if body.grid is not None:
            wall.grid = body.grid
        if body.name is not None:
            wall.name = body.name
        return wall.model_copy()

    wall = await state.update_config(mutate, "walls")
    log.info("Muro %d actualizado por «%s» (cuadrícula %d)", monitor, p.username, wall.grid)
    return json_response(wall, headers={"ETag": f'"{state.repo.revision}"'})
