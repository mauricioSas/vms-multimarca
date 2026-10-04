"""Captura de una imagen JPEG en memoria desde un flujo RTSP local (para dibujar zonas).

El editor de líneas y zonas del panel necesita una imagen de la cámara de fondo. El backend la
pide primero al propio equipo (ISAPI/CGI); si falla, puede usar esta función para tomar un
frame del subflujo en MediaMTX. La imagen se devuelve como bytes y **nunca se escribe en disco**
(RGPD); el backend la sirve con `Cache-Control: no-store`.
"""
from __future__ import annotations

import time

from vms.core.errors import DeviceUnreachable

from .video import open_capture


def grab_jpeg(rtsp_url: str, *, timeout: float = 8.0, quality: int = 85, max_width: int = 1280) -> bytes:
    """Devuelve un JPEG del flujo. Lanza DeviceUnreachable (mensaje en español) si no hay vídeo."""
    import cv2

    deadline = time.monotonic() + timeout
    cap = open_capture(rtsp_url)
    try:
        if not cap.isOpened():
            raise DeviceUnreachable("No se pudo abrir el vídeo de la cámara para la captura")
        image = None
        # El primer frame tras conectar puede venir incompleto: se descartan un par.
        for _ in range(3):
            if time.monotonic() > deadline:
                break
            ok, frame = cap.read()
            if ok and frame is not None:
                image = frame
        if image is None:
            raise DeviceUnreachable("La cámara no envió imagen a tiempo")
        h, w = image.shape[:2]
        if w > max_width:
            image = cv2.resize(image, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            raise DeviceUnreachable("No se pudo codificar la captura")
        return bytes(buf.tobytes())
    finally:
        cap.release()
