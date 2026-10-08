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
from vms.core.errors import EngineError, EngineUnavailable, NotFoundError, ValidationFailed, VmsError
from vms.core.recordings import (PLAYBACK_ENGINE_DOWN, RECORDINGS_DIR_MISSING, classify_playback_error,
                                 playback_error_text, recordings_presence)

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


async def _read_capped(resp: httpx.Response, limit: int) -> bytes:
    out = b""
    async for chunk in resp.aiter_bytes():
        out += chunk
        if len(out) >= limit:
            break
    return out[:limit]


def _local_stamp(dt: datetime, tz_name: str) -> str:
    try:
        tz: Any = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Zona horaria «%s» no disponible; se usa UTC en el nombre del archivo", tz_name)
        tz = timezone.utc
    return dt.astimezone(tz).strftime("%Y%m%d-%H%M%S")


# Los «Qué hacer:» solo nombran pantallas y botones que existen en la web (tests/api/test_recordings_texts.py).
NOTICE_DISABLED = ("Esta cámara está desactivada y no graba. Qué hacer: si quieres que vuelva a grabar, en «Equipos y "
                   "monitores» edita la cámara y marca «Activada».")
# Un equipo no se reactiva desde la web (no hay botón): solo por la API
NOTICE_DEVICE_DISABLED = ("El equipo de esta cámara está desactivado («Desactivado» en «Equipos y monitores») y no "
                          "graba. Qué hacer: si quieres que vuelva a grabar, avisa a soporte para que active el equipo.")
NOTICE_RECORDING_OFF = ("La grabación de esta cámara está desactivada. Qué hacer: en «Equipos y monitores», edita la "
                        "cámara y marca «Grabar 24/7».")
# Sin carpeta de la cámara: nunca grabó o caducó todo lo que tenía (MediaMTX borra la carpeta vacía)
NOTICE_NEVER_RECORDED = ("Esta cámara todavía no ha grabado nada (o sus grabaciones ya se borraron por antigüedad). "
                         "Qué hacer: en «Equipos y monitores», comprueba que la cámara tiene vídeo; si pone «Sin vídeo», "
                         "pulsa «¿Por qué no conecta?» en su equipo. En cuanto llegue vídeo, la grabación aparecerá "
                         "aquí en menos de un minuto.")
NO_RECORDING_THERE = "No hay grabación en ese momento. Qué hacer: elige un tramo de color en la línea de tiempo."
PLAYBACK_REJECTED = ("El motor de vídeo no aceptó esta petición de reproducción. Qué hacer: elige otro momento en la "
                     "línea de tiempo y pulsa «Actualizar»; si sigue igual, avisa a soporte.")


def _in_engine(state: AppState, camera_id: str) -> bool:
    """¿Tiene la cámara su ruta activa en el motor? (cámara y equipo activados)."""
    cfg = state.config()
    cam = cfg.camera(camera_id)
    dev = cfg.device(cam.device_id) if cam is not None else None
    return cam is not None and cam.enabled and dev is not None and dev.enabled


def _timeline_notice(state: AppState, camera_id: str) -> dict[str, str] | None:
    """Por qué la línea de tiempo está vacía, si hay una razón que no sea «ese día no se grabó» (CONTRATO §6.7).

    Una carpeta de grabaciones que no existe entera no es «nunca ha grabado»: es un disco desconectado (502)."""
    cfg = state.config()
    cam = get_camera(cfg, camera_id)
    dev = cfg.device(cam.device_id)
    if dev is None or not dev.enabled:
        return {"code": "camera_disabled", "message": NOTICE_DEVICE_DISABLED}
    if not cam.enabled:
        return {"code": "camera_disabled", "message": NOTICE_DISABLED}
    if not cam.record:
        return {"code": "recording_off", "message": NOTICE_RECORDING_OFF}
    presence = recordings_presence(state.recordings_dir(cfg), camera_id)
    if presence == "no_root":
        raise EngineError(RECORDINGS_DIR_MISSING, details={"reason": "no_root"})
    if presence == "none":
        return {"code": "never_recorded", "message": NOTICE_NEVER_RECORDED}
    return None


@router.get("/summary")
async def summary(p: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    cfg = state.config()
    root = Path(state.recordings_dir(cfg))
    out = []
    visible = visible_camera_ids(state, p, (c.id for c in cfg.cameras), "playback")
    for cam in (c for c in cfg.cameras if c.id in visible):
        try:
            spans = await state.engine.list_recordings(cam.id, None, None)
        except VmsError as exc:   # una cámara con problemas no tumba el resumen de las demás
            log.debug("Resumen de grabaciones sin %s: %s", cam.id, exc.message)
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
    notice = None if spans else await asyncio.to_thread(_timeline_notice, state, camera_id)
    return json_response({"camera_id": camera_id,
                          "spans": [{"start": sp.start, "end": sp.end, "duration": round(sp.duration, 3)}
                                    for sp in spans],
                          "notice": notice})


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
        raise EngineUnavailable("El proxy de vídeo aún no está listo. Qué hacer: espera unos segundos y pulsa "
                                "«Actualizar».")
    req = state.proxy.build_request("GET", url, timeout=httpx.Timeout(30.0, read=60.0))
    try:
        upstream = await state.proxy.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise EngineUnavailable(PLAYBACK_ENGINE_DOWN, details={"reason": type(exc).__name__}) from exc
    if upstream.status_code != 200:
        # Hasta 4 KB: con una carpeta de grabaciones larga, «cannot find the path specified» queda al final
        body = await _read_capped(upstream, 4096)
        await upstream.aclose()
        error = playback_error_text(body)
        problem = classify_playback_error(upstream.status_code, error)
        if problem is None:   # 404 o carpeta de la cámara inexistente (nunca grabó): no hay grabación
            raise NotFoundError(NO_RECORDING_THERE)
        if problem.details.get("kind") == "not_configured" and not _in_engine(state, camera_id):
            # Desactivada y sin grabaciones: ni siquiera tiene ruta inerte (§4.3). «Espera unos segundos» sería falso
            raise NotFoundError(NO_RECORDING_THERE)
        if upstream.status_code == 400 and problem.details.get("kind") == "other":
            return error_response("playback_rejected", PLAYBACK_REJECTED, 400, {"reason": error[:200]})
        raise problem

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
