"""Cámaras (canales de vídeo) y snapshot en memoria (CONTRATO §6.4)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import ConflictError, DeviceError, NotFoundError, RateLimited, ValidationFailed
from vms.core.models import AppConfig, Camera, CameraCreate, CameraUpdate, Device
from vms.core.mtx_auth import with_reader_credentials

from ..deps import Principal, get_state, require_admin, require_kiosk
from ..errors import json_response
from ..security import client_ip
from ..state import AppState
from ..views import camera_out

log = logging.getLogger("vms.api.cameras")
router = APIRouter(prefix="/api/cameras", tags=["cameras"])

SNAPSHOT_MIN_INTERVAL_S = 1.0


def get_camera(cfg: AppConfig, camera_id: str) -> Camera:
    cam = cfg.camera(camera_id)
    if cam is None:
        raise NotFoundError("La cámara no existe")
    return cam


def _validate(cfg: AppConfig, cam: Camera) -> None:
    dev = cfg.device(cam.device_id)
    if dev is None:
        raise ValidationFailed("El equipo indicado no existe",
                               details={"fields": [{"loc": ["device_id"], "msg": "El equipo no existe"}]})
    for other in cfg.cameras_of(cam.device_id):
        if other.id != cam.id and other.channel == cam.channel:
            raise ConflictError(f"El canal {cam.channel} de «{dev.name}» ya está dado de alta como «{other.name}»")
    if dev.vendor in ("onvif", "generic") and not cam.main_path:
        raise ValidationFailed("Indica la ruta RTSP principal (los equipos ONVIF o genéricos no tienen preset)",
                               details={"fields": [{"loc": ["main_path"], "msg": "Campo obligatorio"}]})


@router.get("")
async def list_cameras(_: Principal = Depends(require_kiosk), state: AppState = Depends(get_state)) -> Response:
    cfg = state.config()
    paths = await state.paths_status_safe()
    return json_response([camera_out(cfg, c, paths) for c in cfg.cameras])


@router.post("")
async def create_camera(body: CameraCreate, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state)) -> Response:
    cam = Camera(**body.model_dump())

    def mutate(cfg: AppConfig) -> None:
        _validate(cfg, cam)
        cfg.cameras.append(cam)

    await state.update_config(mutate, "cameras")
    log.info("Cámara «%s» creada por «%s»", cam.name, p.username)
    return json_response(camera_out(state.config(), cam, await state.paths_status_safe()), 201)


@router.get("/{camera_id}")
async def get_one(camera_id: str, _: Principal = Depends(require_kiosk), state: AppState = Depends(get_state)) -> Response:
    cfg = state.config()
    return json_response(camera_out(cfg, get_camera(cfg, camera_id), await state.paths_status_safe()))


@router.patch("/{camera_id}")
async def update_camera(camera_id: str, body: CameraUpdate, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state)) -> Response:
    fields = body.model_fields_set
    nullable = {"main_path", "sub_path"}  # null = volver al preset del fabricante
    changes = {k: getattr(body, k) for k in fields if getattr(body, k) is not None or k in nullable}

    def mutate(cfg: AppConfig) -> Camera:
        cam = get_camera(cfg, camera_id)
        updated = Camera.model_validate({**cam.model_dump(), **changes, "updated_at": datetime.now(timezone.utc)})
        _validate(cfg, updated)
        cfg.cameras = [updated if c.id == camera_id else c for c in cfg.cameras]
        return updated

    updated = await state.update_config(mutate, "cameras")
    log.info("Cámara «%s» modificada por «%s» (%s)", updated.name, p.username, ", ".join(sorted(changes)) or "-")
    return json_response(camera_out(state.config(), updated, await state.paths_status_safe()))


@router.delete("/{camera_id}")
async def delete_camera(camera_id: str, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state)) -> Response:
    def mutate(cfg: AppConfig) -> Camera:
        cam = get_camera(cfg, camera_id)
        cfg.remove_camera(camera_id)
        return cam

    cam = await state.update_config(mutate, "cameras")
    log.info("Cámara «%s» borrada por «%s»", cam.name, p.username)
    return Response(status_code=204)


def _grab_frame(url: str, timeout_ms: int = 5000) -> bytes | None:
    """Un frame del RTSP local de MediaMTX, codificado a JPEG en memoria (nunca toca el disco).

    Usa analytics.snapshot.grab_jpeg (CONTRATO §8.8) si está instalado el extra de analítica;
    si no, OpenCV directamente; sin OpenCV no hay respaldo.
    """
    try:
        from analytics.snapshot import grab_jpeg
    except ImportError:
        grab_jpeg = None  # type: ignore[assignment]
    if grab_jpeg is not None:
        try:
            return grab_jpeg(url, timeout=timeout_ms / 1000)
        except DeviceError as exc:
            log.info("Sin imagen del vídeo local: %s", exc.message)
            return None
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture()
    try:
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms]
        if not cap.open(url, cv2.CAP_FFMPEG, params):
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return bytes(buf) if ok else None
    finally:
        cap.release()


@router.get("/{camera_id}/snapshot")
async def snapshot(request: Request, camera_id: str, stream: Literal["sub", "main"] = "sub",
                   p: Principal = Depends(require_admin), state: AppState = Depends(get_state)) -> Response:
    cfg = state.config()
    cam = get_camera(cfg, camera_id)
    audit("snapshot", user=p.username, ip=client_ip(request), camera_id=camera_id, camera=cam.name, stream=stream)
    dev: Device | None = cfg.device(cam.device_id)
    now = time.monotonic()
    if now - state.snapshot_last.get(camera_id, -1e9) < SNAPSHOT_MIN_INTERVAL_S:
        raise RateLimited("Espera un segundo entre capturas de la misma cámara", details={"retry_after": 1})
    state.snapshot_last[camera_id] = now
    if stream == "sub" and not cam.has_sub:
        stream = "main"
    image: bytes | None = None
    device_error: DeviceError | None = None
    if dev is not None and dev.vendor != "generic":
        client = state.client_factory(dev, state.creds.get_device_password(dev.id))
        try:
            image = await client.snapshot(cam.channel, stream)
        except DeviceError as exc:
            device_error = exc
            log.info("Snapshot del equipo no disponible para «%s»: %s; se intenta con el vídeo local",
                     cam.name, exc.message)
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                log.debug("Error cerrando el cliente del equipo", exc_info=True)
    if image is None:
        try:
            url = state.engine.rtsp_read_url(camera_id, stream)
            creds = getattr(state.engine, "mtx_credentials", None)
            if creds is not None:
                url = with_reader_credentials(url, creds, state.settings.mtx_rtsp_address)
            image = await asyncio.to_thread(_grab_frame, url)
        except Exception:  # noqa: BLE001 - el respaldo nunca debe romper la petición
            log.exception("Error capturando un frame del vídeo local de «%s»", cam.name)
            image = None
    if image is None:
        if device_error is not None:
            raise device_error
        raise DeviceError("No se pudo obtener una imagen de la cámara (ni del equipo ni del vídeo local)")
    return Response(image, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
