"""Obtener una imagen del subflujo para la salud (CONTRATO §18.2): primero `DeviceClient.snapshot` y, si
falla, un fotograma del RTSP local de MediaMTX (igual que `/api/cameras/{id}/snapshot`). Nunca se
decodifica de forma continua y la imagen nunca toca el disco.

Contraseña rechazada (`DeviceAuthFailed`): la API de ese equipo no se vuelve a usar hasta que cambie la
contraseña guardada o pasen 30 minutos (la ventana de bloqueo de Hikvision/Dahua tras 5 intentos); mientras
tanto la imagen sale solo del RTSP local de MediaMTX. Así la tarea de salud (una petición por cámara cada
3 minutos) no bloquea la cuenta del equipo, igual que el diagnóstico hace un único intento con credenciales.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import Literal

from vms.core.errors import DeviceAuthFailed, DeviceError
from vms.core.models import Camera
from vms.core.mtx_auth import with_reader_credentials

from ..host import OpsHost
from .imaging import Image, decode_image

log = logging.getLogger("vms.ops.snapshot")

AUTH_BACKOFF_S = 30 * 60


class AuthBackoff:
    """Equipos cuya contraseña guardada se ha rechazado. Solo en memoria; guarda una huella de la contraseña
    (nunca la contraseña) para saber si se ha cambiado."""

    def __init__(self, seconds: float = AUTH_BACKOFF_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self.clock = clock
        self._blocked: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _fp(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def blocked(self, device_id: str, password: str) -> bool:
        entry = self._blocked.get(device_id)
        if entry is None:
            return False
        fp, until = entry
        if fp != self._fp(password) or self.clock() >= until:
            del self._blocked[device_id]
            return False
        return True

    def block(self, device_id: str, password: str) -> None:
        if device_id not in self._blocked:
            log.warning("El equipo %s rechaza la contraseña guardada: la salud no usará su API durante %d min "
                        "(solo el vídeo local) para no bloquear la cuenta", device_id, int(self.seconds // 60))
        self._blocked[device_id] = (self._fp(password), self.clock() + self.seconds)


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


_default_backoff = AuthBackoff()


async def grab(state: OpsHost, cam: Camera, backoff: AuthBackoff | None = None) -> Image | None:
    backoff = backoff or _default_backoff
    cfg = state.config()
    dev = cfg.device(cam.device_id)
    stream: Literal["sub", "main"] = "sub" if cam.has_sub else "main"
    password = state.creds.get_device_password(dev.id) if dev is not None and dev.vendor != "generic" else ""
    if dev is not None and dev.vendor != "generic" and not backoff.blocked(dev.id, password):
        client = state.client_factory(dev, password)
        try:
            img = decode_image(await client.snapshot(cam.channel, stream))
            if img is not None:
                return img
        except DeviceAuthFailed:
            backoff.block(dev.id, password)
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
