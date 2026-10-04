"""Grabaciones: línea de tiempo y vídeo por proxy del servidor de reproducción (CONTRATO §6.7)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from vms.core.audit import audit
from vms.core.errors import EngineUnavailable, NotFoundError, ValidationFailed

from ..deps import Principal, get_state, require_operator
from ..errors import error_response, json_response
from ..security import client_ip
from ..permissions import ensure_camera_access, visible_camera_ids
from ..state import AppState
from .cameras import get_camera

log = logging.getLogger("vms.api.recordings")
router = APIRouter(prefix="/api/recordings", tags=["recordings"])

MAX_DURATION_S = 3600.0


def utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                continue
    return total


def _ascii_name(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", plain).strip("_") or "camara"


def _local_stamp(dt: datetime, tz_name: str) -> str:
    try:
        tz: Any = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Zona horaria «%s» no disponible; se usa UTC en el nombre del archivo", tz_name)
        tz = timezone.utc
    return dt.astimezone(tz).strftime("%Y%m%d-%H%M%S")


@router.get("/summary")
async def summary(p: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    cfg = state.config()
    root = Path(state.recordings_dir(cfg))
    out = []
    visible = visible_camera_ids(state, p, (c.id for c in cfg.cameras), "playback")
    for cam in (c for c in cfg.cameras if c.id in visible):
        try:
            spans = await state.engine.list_recordings(cam.id, None, None)
        except EngineUnavailable:
            spans = []
        size = await asyncio.to_thread(_dir_size, root / cam.id)
        out.append({"camera_id": cam.id, "name": cam.name,
                    "first": spans[0].start if spans else None,
                    "last": max(s.end for s in spans) if spans else None, "bytes": size})
    return json_response(out)


@router.get("/{camera_id}/timeline")
async def timeline(camera_id: str, start: datetime | None = None, end: datetime | None = None,
                   p: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    ensure_camera_access(state, p, camera_id, "playback")
    get_camera(state.config(), camera_id)
    s, e = utc(start), utc(end)
    if s and e and e <= s:
        raise ValidationFailed("El final debe ser posterior al inicio")
    spans = await state.engine.list_recordings(camera_id, s, e)
    return json_response({"camera_id": camera_id,
                          "spans": [{"start": sp.start, "end": sp.end, "duration": round(sp.duration, 3)}
                                    for sp in spans]})


@router.get("/{camera_id}/video")
async def video(request: Request, camera_id: str, start: datetime, duration: float = Query(..., gt=0),
                format: Literal["fmp4", "mp4"] = "fmp4", download: bool = False,
                p: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    ensure_camera_access(state, p, camera_id, "export" if download else "playback")
    cam = get_camera(state.config(), camera_id)
    if duration > MAX_DURATION_S:
        raise ValidationFailed(f"La duración máxima es {int(MAX_DURATION_S)} segundos",
                               details={"fields": [{"loc": ["duration"], "msg": "Máximo 3600 s"}]})
    start_utc = utc(start)
    assert start_utc is not None
    audit("recording_download" if download else "recording_view", user=p.username,
          ip=client_ip(request), camera_id=camera_id, camera=cam.name,
          start=start_utc.isoformat(), duration_s=round(duration, 3), format=format)
    url = state.engine.playback_get_url(camera_id, start_utc, duration, format)
    if state.proxy is None:
        raise EngineUnavailable("El proxy de vídeo no está listo")
    req = state.proxy.build_request("GET", url, timeout=httpx.Timeout(30.0, read=60.0))
    try:
        upstream = await state.proxy.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise EngineUnavailable("El servidor de reproducción no responde") from exc
    if upstream.status_code != 200:
        body = (await upstream.aread())[:300].decode("utf-8", errors="replace")
        await upstream.aclose()
        if upstream.status_code == 404:
            raise NotFoundError("No hay grabación en ese momento")
        if upstream.status_code == 400:
            return error_response("playback_rejected", "Petición de reproducción no válida", 400, {"reason": body})
        return error_response("engine_error", "El servidor de reproducción devolvió un error", 502,
                              {"status": upstream.status_code})

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        except httpx.HTTPError as exc:
            log.warning("Se cortó la reproducción de %s: %s", camera_id, type(exc).__name__)

    headers = {"Cache-Control": "no-store"}
    if upstream.headers.get("content-length"):
        headers["Content-Length"] = upstream.headers["content-length"]
    if download:
        stamp = _local_stamp(start_utc, state.site().timezone)
        fname = f"{cam.name}_{stamp}.mp4"
        headers["Content-Disposition"] = (f'attachment; filename="{_ascii_name(cam.name)}_{stamp}.mp4"; '
                                          f"filename*=UTF-8''{quote(fname)}")
    return StreamingResponse(body_iter(), media_type="video/mp4", headers=headers,
                             background=BackgroundTask(upstream.aclose))
