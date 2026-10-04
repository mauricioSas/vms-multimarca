"""Analítica de tienda: conteo en puerta (cruce de línea) y ocupación de cola en cajas.

Dueño: agente «analytics». Contrato: docs/CONTRATO.md §8.
RGPD: procesa cada frame en memoria y lo descarta; nunca guarda imágenes ni identifica personas.
"""
import sys as _sys

# PyAV (wheels de PyPI con FFmpeg GPL) está prohibido. supervision solo lo importa en sus
# utilidades de vídeo, que no usamos; si algo intentara importarlo, fallará de forma explícita.
_sys.modules.setdefault("av", None)  # type: ignore[arg-type]

# Privacidad: el paquete «openvino» envía un evento de uso a Google Analytics al importarse
# (lo hace openvino.tools.ovc a través de «openvino-telemetry», comprobado con la 2026.4).
# Un PC de tienda no debe enviar nada a terceros: se bloquea el módulo antes de que nadie lo
# importe. OpenVINO funciona igual sin él (verificado: lectura de ONNX/IR e inferencia).
_sys.modules.setdefault("openvino_telemetry", None)  # type: ignore[arg-type]

# OpenCV/FFmpeg escriben sus propios avisos directamente en la consola (fuera de nuestros logs,
# que ya registran cada fallo de vídeo en español). Se silencian antes de importar cv2.
import os as _os

_os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
_os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")   # AV_LOG_QUIET
# Lectura RTSP por TCP con 5 s de espera sin datos (se lee al abrir cada flujo).
_os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;5000000")

__all__: list[str] = []
