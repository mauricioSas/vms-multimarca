"""Server-Sent Events: cambios de configuración y estado cada 5 s (CONTRATO §6.11).

Cada conexión recibe solo lo que su usuario puede ver (revisión v2, ámbito por cámara, CONTRATO §18.8):
- `status`: solo las cámaras que puede ver en vivo;
- `health` y `bookmark`: solo de cámaras de su ámbito (`live` y `playback`);
- `notice`: solo si todas sus cámaras están en su ámbito (el título lleva el nombre de la cámara);
- `evidence`: solo a quien pidió la exportación (o a un administrador) mientras pueda exportar esas cámaras;
- los muros en kiosco: ni `notice`, ni `health`, ni `bookmark`, ni `evidence` (§18.9);
- `config`, `engine` y `update` son de la tienda y van a todos.
Las claves que empiezan por «_» son para este filtro y no salen al navegador.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from ..deps import Principal, get_state, require_kiosk
from ..events import format_sse
from ..permissions import camera_allowed
from ..state import AppState

log = logging.getLogger("vms.api.events")
router = APIRouter(prefix="/api", tags=["events"])

STATUS_EVERY_S = 5.0
PING_EVERY_S = 15.0


async def status_event(state: AppState, principal: Principal | None = None) -> bytes:
    """Evento `status`; con `principal`, solo las cámaras que ese usuario puede ver en vivo."""
    paths = await state.paths_status_safe()
    try:
        running = (await state.engine.status()).running
    except Exception:  # noqa: BLE001
        log.debug("Estado del motor no disponible para SSE", exc_info=True)
        running = False
    cfg = state.config()
    cams = []
    for cam in cfg.cameras:
        if principal is not None and not camera_allowed(state, principal, cam.id, "live"):
            continue
        main = (paths or {}).get(f"{cam.id}/main")
        cams.append({"camera_id": cam.id, "online": bool(main and main.ready),
                     "recording": bool(main and main.recording)})
    data = json.dumps({"cameras": cams, "engine": {"running": running}}, separators=(",", ":"))
    return format_sse("status", data)


KIOSK_HIDDEN = frozenset({"notice", "health", "bookmark", "evidence"})


def scoped_event(state: AppState, p: Principal, event: str, payload: str) -> bytes | None:
    """El evento tal como lo puede ver `p` (sin las claves internas «_…»), o None si no le corresponde."""
    if p.kiosk and event in KIOSK_HIDDEN:
        return None
    if event not in ("health", "bookmark", "notice", "evidence") and '"_' not in payload:
        return format_sse(event, payload)
    try:
        data: dict[str, Any] = json.loads(payload)
    except ValueError:
        return None
    if p.role != "admin":
        if event == "health" and not camera_allowed(state, p, str(data.get("camera_id", "")), "live"):
            return None
        if event == "bookmark" and not camera_allowed(state, p, str(data.get("camera_id", "")), "playback"):
            return None
        if event == "notice" and not all(camera_allowed(state, p, str(c), "live")
                                         for c in data.get("camera_ids") or []):
            return None
        if event == "evidence":
            cams = data.get("_cameras") or []
            if data.get("_owner") != p.username or not cams or \
                    not all(camera_allowed(state, p, str(c), "export") for c in cams):
                return None
    public = {k: v for k, v in data.items() if not k.startswith("_")}
    return format_sse(event, json.dumps(public, ensure_ascii=False, separators=(",", ":")))


@router.get("/events")
async def events(request: Request, p: Principal = Depends(require_kiosk),
                 state: AppState = Depends(get_state)) -> StreamingResponse:
    queue = state.bus.subscribe()

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b"retry: 3000\n\n"
            yield await status_event(state, p)
            next_status = time.monotonic() + STATUS_EVERY_S
            next_ping = time.monotonic() + PING_EVERY_S
            while True:
                if await request.is_disconnected():
                    return
                wait = max(0.05, min(next_status, next_ping) - time.monotonic())
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=min(wait, 1.0))
                    out = scoped_event(state, p, event, data)
                    if out is not None:
                        yield out
                    continue
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                if now >= next_status:
                    yield await status_event(state, p)
                    next_status = now + STATUS_EVERY_S
                if now >= next_ping:
                    yield b": ping\n\n"
                    next_ping = now + PING_EVERY_S
        finally:
            state.bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
