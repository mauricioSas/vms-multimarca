"""Server-Sent Events: cambios de configuración y estado cada 5 s (CONTRATO §6.11)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from ..deps import Principal, get_state, require_kiosk
from ..events import format_sse
from ..state import AppState

log = logging.getLogger("vms.api.events")
router = APIRouter(prefix="/api", tags=["events"])

STATUS_EVERY_S = 5.0
PING_EVERY_S = 15.0


async def status_event(state: AppState) -> bytes:
    paths = await state.paths_status_safe()
    try:
        running = (await state.engine.status()).running
    except Exception:  # noqa: BLE001
        log.debug("Estado del motor no disponible para SSE", exc_info=True)
        running = False
    cfg = state.config()
    cams = []
    for cam in cfg.cameras:
        main = (paths or {}).get(f"{cam.id}/main")
        cams.append({"camera_id": cam.id, "online": bool(main and main.ready),
                     "recording": bool(main and main.recording)})
    data = json.dumps({"cameras": cams, "engine": {"running": running}}, separators=(",", ":"))
    return format_sse("status", data)


@router.get("/events")
async def events(request: Request, _: Principal = Depends(require_kiosk),
                 state: AppState = Depends(get_state)) -> StreamingResponse:
    queue = state.bus.subscribe()

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b"retry: 3000\n\n"
            yield await status_event(state)
            next_status = time.monotonic() + STATUS_EVERY_S
            next_ping = time.monotonic() + PING_EVERY_S
            while True:
                if await request.is_disconnected():
                    return
                wait = max(0.05, min(next_status, next_ping) - time.monotonic())
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=min(wait, 1.0))
                    yield format_sse(event, data)
                    continue
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                if now >= next_status:
                    yield await status_event(state)
                    next_status = now + STATUS_EVERY_S
                if now >= next_ping:
                    yield b": ping\n\n"
                    next_ping = now + PING_EVERY_S
        finally:
            state.bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
