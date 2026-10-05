"""Obtener una imagen del subflujo para la salud (CONTRATO §18.2): primero `DeviceClient.snapshot` y, si
falla, un fotograma del RTSP local de MediaMTX (igual que `/api/cameras/{id}/snapshot`). Nunca se
decodifica de forma continua y la imagen nunca toca el disco."""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from vms.core.errors import DeviceError
from vms.core.models import Camera
from vms.core.mtx_auth import with_reader_credentials

from ..host import OpsHost
from .imaging import Image, decode_image

log = logging.getLogger("vms.ops.snapshot")


def grab_rtsp_frame(url: str, timeout_ms: int = 5000) -> Image | None:
    try:
        import cv2
    except ImportError:   # pragma: no cover - OpenCV va en el extra [vms]
        return None
    cap = cv2.VideoCapture()
    try:
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms]
        if not cap.open(url, cv2.CAP_FFMPEG, params):
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        out: Image = frame.astype("uint8", copy=False)
        return out
    finally:
        cap.release()


async def grab(state: OpsHost, cam: Camera) -> Image | None:
    cfg = state.config()
    dev = cfg.device(cam.device_id)
    stream: Literal["sub", "main"] = "sub" if cam.has_sub else "main"
    if dev is not None and dev.vendor != "generic":
        client = state.client_factory(dev, state.creds.get_device_password(dev.id))
        try:
            img = decode_image(await client.snapshot(cam.channel, stream))
            if img is not None:
                return img
        except DeviceError as exc:
            log.debug("Sin instantánea del equipo para %s: %s", cam.id, exc.message)
        except Exception:  # noqa: BLE001 - un driver roto no puede tumbar la comprobación
            log.exception("Error pidiendo la instantánea de %s", cam.id)
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                log.debug("Error cerrando el cliente del equipo", exc_info=True)
    try:
        url = state.engine.rtsp_read_url(cam.id, stream)
        creds = getattr(state.engine, "mtx_credentials", None)
        if creds is not None:
            url = with_reader_credentials(url, creds, state.settings.mtx_rtsp_address)
        return await asyncio.to_thread(grab_rtsp_frame, url)
    except Exception:  # noqa: BLE001
        log.debug("Sin fotograma del vídeo local de %s", cam.id, exc_info=True)
        return None
