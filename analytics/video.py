"""Lectura del subflujo RTSP con el patrón «solo el último frame».

Cómo funciona, en sencillo:
- Una cámara envía, por ejemplo, 15 imágenes (frames) por segundo. El detector quizá solo
  procesa 10 por segundo en la puerta o 1 por segundo en cajas.
- Si leyéramos los frames en orden y uno a uno, se acumularían en una cola y el conteo iría
  cada vez más retrasado respecto a la realidad.
- Por eso un hilo «lector» lee sin parar y se queda solo con el ÚLTIMO frame (pisa el anterior).
  El procesador, cuando está libre, toma ese último frame. Lo que no se procesa se descarta.

Siempre se lee de MediaMTX (127.0.0.1), nunca directamente del NVR: así el NVR mantiene una
sola conexión por canal aunque haya vista en vivo, grabación y analítica a la vez.

RGPD: los frames solo viven en memoria. Este módulo nunca escribe imágenes a disco.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from vms.core.rtsp import redact

log = logging.getLogger("analytics.video")

# OpenCV usa FFmpeg por debajo. Estas opciones se leen al abrir cada flujo:
#  - rtsp_transport;tcp: TCP en vez de UDP (sin pérdidas por la red de la tienda)
#  - timeout: segundos (en microsegundos) sin datos antes de dar el flujo por caído
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;5000000")

BACKOFF_STEPS = (1.0, 2.0, 5.0, 10.0, 30.0)
OPEN_TIMEOUT_MS = 8000
READ_TIMEOUT_MS = 8000

GrabberState = Literal["connecting", "running", "error", "stopped"]


@dataclass(frozen=True)
class Frame:
    image: np.ndarray            # BGR, en memoria
    wall_time: float             # time.time() al recibirlo (para el minuto UTC)
    mono_time: float             # time.monotonic() al recibirlo (para medir duraciones)
    seq: int                     # número de frame recibido (sube de 1 en 1)


class Backoff:
    """Esperas crecientes entre reintentos: 1, 2, 5, 10, 30, 30... segundos."""

    def __init__(self, steps: tuple[float, ...] = BACKOFF_STEPS) -> None:
        self._steps = steps
        self._i = 0

    def next(self) -> float:
        delay = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        return delay

    def reset(self) -> None:
        self._i = 0


class RateMeter:
    """Frecuencia (eventos por segundo) en una ventana deslizante de unos segundos."""

    def __init__(self, window_s: float = 5.0) -> None:
        self._window = window_s
        self._times: list[float] = []
        self._lock = threading.Lock()

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._times.append(now)
            cutoff = now - self._window
            drop = 0
            while drop < len(self._times) and self._times[drop] < cutoff:
                drop += 1
            if drop:
                del self._times[:drop]

    def rate(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._times if t >= now - self._window]
        if len(recent) < 2 or now - recent[-1] > 2.0:
            return 0.0   # sin datos suficientes, o el flujo lleva más de 2 s parado
        return (len(recent) - 1) / max(recent[-1] - recent[0], 1e-6)


def open_capture(url: str) -> Any:
    """Abre un VideoCapture de OpenCV/FFmpeg con tiempos de espera. Devuelve el objeto (abierto o no)."""
    import cv2

    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS]
    try:
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
    except cv2.error:  # versiones sin parámetros en el constructor
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG)


class FrameGrabber:
    """Hilo lector de una cámara: conecta, lee, reconecta con espera creciente si se cae."""

    def __init__(self, url: str, name: str = "", *,
                 capture_factory: Callable[[str], Any] = open_capture,
                 backoff: tuple[float, ...] = BACKOFF_STEPS) -> None:
        self.url = url
        self.name = name or redact(url)
        self._factory = capture_factory
        self._backoff = Backoff(backoff)
        self._cond = threading.Condition()
        self._latest: Frame | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: GrabberState = "connecting"
        self.last_error = ""
        self.reconnects = 0
        self.frame_size: tuple[int, int] | None = None   # (ancho, alto)
        self.input_rate = RateMeter()

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> "FrameGrabber":
        self._thread = threading.Thread(target=self._run, name=f"grab-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("[%s] el hilo lector no terminó a tiempo (lectura bloqueada)", self.name)
        self.state = "stopped"

    # ------------------------------------------------------------------ consumo
    def latest(self) -> Frame | None:
        with self._cond:
            return self._latest

    def wait_newer(self, after_seq: int, timeout: float) -> Frame | None:
        """Espera hasta que haya un frame más nuevo que `after_seq` (o vence el plazo)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._stop.is_set():
                if self._latest is not None and self._latest.seq > after_seq:
                    return self._latest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
        return None

    # ------------------------------------------------------------------ hilo
    def _run(self) -> None:
        while not self._stop.is_set():
            self.state = "connecting"
            cap = None
            try:
                cap = self._factory(self.url)
                if cap is None or not cap.isOpened():
                    raise ConnectionError("no se pudo abrir el flujo RTSP")
                log.info("[%s] conectado a %s", self.name, redact(self.url))
                self._read_loop(cap)
            except Exception as exc:  # cualquier fallo de OpenCV/FFmpeg: se registra y se reintenta
                self.last_error = redact(f"{type(exc).__name__}: {exc}")
                self.state = "error"
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception as exc:
                        log.debug("[%s] error al liberar el flujo: %s", self.name, exc)
            if self._stop.is_set():
                break
            delay = self._backoff.next()
            self.reconnects += 1
            log.warning("[%s] sin vídeo (%s); reintento en %.0f s", self.name, self.last_error, delay)
            self._stop.wait(delay)

    def _read_loop(self, cap: Any) -> None:
        good_since: float | None = None
        while not self._stop.is_set():
            ok, image = cap.read()
            if not ok or image is None:
                raise ConnectionError("el flujo dejó de enviar imágenes")
            now_m = time.monotonic()
            if good_since is None:
                good_since = now_m
                self.state = "running"
                self.last_error = ""
            elif now_m - good_since > 30.0:
                self._backoff.reset()  # 30 s estables: la próxima caída vuelve a esperar poco
            h, w = image.shape[:2]
            self.frame_size = (int(w), int(h))
            self.input_rate.tick(now_m)
            with self._cond:
                self._seq += 1
                # Se pisa el frame anterior: el viejo queda sin referencias y Python lo libera.
                self._latest = Frame(image, time.time(), now_m, self._seq)
                self._cond.notify_all()
