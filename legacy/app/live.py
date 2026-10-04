"""Vista en vivo: un hilo por flujo RTSP (subflujo) que decodifica con PyAV/FFmpeg.

Patrón «solo el último frame»:
  - El hilo decodifica todos los paquetes (H.264/H.265 lo exige), pero solo convierte a
    RGB y escala al tamaño de la celda a la cadencia de pantalla (15-20 fps). El escalado
    se hace aquí, fuera del hilo de la interfaz.
  - El resultado se guarda en un único atributo (secuencia, imagen). No hay colas: si la
    interfaz no ha pintado el frame anterior, se sobrescribe. El retraso no puede crecer.
  - La interfaz lee el último frame con un QTimer y solo de su propio objeto LiveStream,
    así un hilo viejo que aún no ha terminado nunca puede pintar en una celda reasignada.
  - RTSP siempre por TCP, con tiempo máximo de apertura y de lectura.
  - Si el retraso acumulado frente al reloj supera unos segundos (PC saturado), se
    reconecta para volver a tiempo real.
"""
from __future__ import annotations

import enum
import errno
import logging
import threading
import time
from dataclasses import dataclass

from .rtsp import redact

log = logging.getLogger(__name__)

try:
    import av
    import av.error
except Exception:  # pragma: no cover
    av = None


class State(str, enum.Enum):
    CONNECTING = "conectando"
    LIVE = "en vivo"
    RECONNECTING = "reconectando"
    ERROR = "error"
    STOPPED = "detenido"


RTSP_OPTIONS = {
    "rtsp_transport": "tcp",      # evita los artefactos grises de UDP
    "timeout": "8000000",          # timeout de socket en microsegundos
    "allowed_media_types": "video",
    "fflags": "nobuffer",
    "flags": "low_delay",
    "max_delay": "500000",
    "analyzeduration": "2000000",
    "probesize": "2000000",
}

BACKOFF = (1, 2, 4, 8, 15)
AUTH_BACKOFF = 60   # muchas cámaras bloquean el usuario tras varios intentos fallidos
NOT_FOUND_BACKOFF = 30
MAX_LAG_SECONDS = 5.0


def classify_error(exc: BaseException) -> tuple[str, bool, int]:
    """(texto para el usuario, es_error_definitivo, espera_en_segundos)."""
    if av is not None:
        if isinstance(exc, av.error.HTTPUnauthorizedError):
            return "usuario o contraseña incorrectos (401)", True, AUTH_BACKOFF
        if isinstance(exc, av.error.HTTPForbiddenError):
            return "acceso denegado (403)", True, AUTH_BACKOFF
        if isinstance(exc, av.error.HTTPNotFoundError):
            return "ruta RTSP no encontrada (404); revisa canal y fabricante", True, NOT_FOUND_BACKOFF
    en = getattr(exc, "errno", None)
    if isinstance(exc, ConnectionRefusedError) or en == errno.ECONNREFUSED:
        return "conexión rechazada (¿puerto RTSP correcto?)", False, 0
    if isinstance(exc, TimeoutError) or en in (errno.ETIMEDOUT,) or (av and isinstance(exc, av.error.ExitError)):
        return "sin respuesta (tiempo agotado)", False, 0
    msg = redact(str(exc)) or type(exc).__name__
    return msg[:160], False, 0


@dataclass
class StreamInfo:
    width: int = 0
    height: int = 0
    codec: str = ""
    fps_in: float = 0.0


class LiveStream:
    def __init__(self, url: str, label: str = "", display_fps: int = 15,
                 open_timeout: float = 8.0, read_timeout: float = 8.0):
        self._url = url
        self.label = label
        self.display_fps = display_fps
        self.open_timeout = open_timeout
        self.read_timeout = read_timeout
        self.state = State.CONNECTING
        self.detail = ""
        self.info = StreamInfo()
        self.reconnects = 0
        self._had_video = False
        self._latest: tuple[int, object] = (0, None)  # (secuencia, ndarray RGB)
        self._targets: dict[int, tuple[int, int]] = {}
        self._targets_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name=f"live-{label or 'stream'}")

    # --- API para la interfaz ------------------------------------------
    def start(self) -> "LiveStream":
        self._thread.start()
        return self

    def stop(self) -> None:
        """No bloquea: el hilo termina en cuanto vuelve de la lectura en curso."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def set_target(self, token: int, width: int, height: int) -> None:
        with self._targets_lock:
            if width > 0 and height > 0:
                self._targets[token] = (int(width), int(height))
            else:
                self._targets.pop(token, None)

    def latest(self) -> tuple[int, object]:
        return self._latest

    # --- hilo ------------------------------------------------------------
    def _target_size(self, src_w: int, src_h: int) -> tuple[int, int] | None:
        with self._targets_lock:
            if not self._targets:
                return None
            tw = max(w for w, _ in self._targets.values())
            th = max(h for _, h in self._targets.values())
        scale = min(tw / src_w, th / src_h, 1.0)  # nunca agrandar: lo hace la GPU al pintar
        w = max(2, int(src_w * scale) // 2 * 2)
        h = max(2, int(src_h * scale) // 2 * 2)
        return w, h

    def _wait(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _run(self) -> None:
        if av is None:
            self.state, self.detail = State.ERROR, "PyAV no está instalado"
            return
        attempt = 0
        while not self._stop.is_set():
            if self.state != State.ERROR:
                self.state = State.CONNECTING if self.reconnects == 0 else State.RECONNECTING
            self._had_video = False
            wait = 0
            try:
                self._session()
                if self._stop.is_set():
                    break
                self.detail = "el flujo terminó"
            except Exception as exc:  # cualquier fallo de red o decodificación
                if self._stop.is_set():
                    break
                text, fatal, wait = classify_error(exc)
                self.detail = text
                self.state = State.ERROR if fatal else State.RECONNECTING
                log.info("Vista %s: %s", self.label, text)
            if self.state == State.LIVE:
                self.state = State.RECONNECTING
            if self._had_video:
                attempt = 0
            if not wait:
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            self.reconnects += 1
            self._wait(wait)
        self.state = State.STOPPED
        self._latest = (self._latest[0] + 1, None)

    def _session(self) -> None:
        container = av.open(self._url, options=dict(RTSP_OPTIONS),
                            timeout=(self.open_timeout, self.read_timeout))
        try:
            if not container.streams.video:
                raise RuntimeError("el flujo no tiene vídeo")
            vs = container.streams.video[0]
            vs.thread_type = "SLICE"  # multihilo sin añadir frames de retraso
            self.info.codec = vs.codec_context.name
            interval = 1.0 / max(1, self.display_fps)
            last_conv = 0.0
            wall0 = pts0 = None
            n_frames, t_count = 0, time.monotonic()
            for packet in container.demux(vs):
                if self._stop.is_set():
                    return
                for frame in packet.decode():
                    now = time.monotonic()
                    if self.state != State.LIVE:
                        self.state, self.detail = State.LIVE, ""
                        self._had_video = True
                    self.info.width, self.info.height = frame.width, frame.height
                    n_frames += 1
                    if now - t_count >= 2.0:
                        self.info.fps_in = n_frames / (now - t_count)
                        n_frames, t_count = 0, now
                    # control de retraso acumulado
                    ft = frame.time
                    if ft is not None:
                        if wall0 is None:
                            wall0, pts0 = now, ft
                        else:
                            lag = (now - wall0) - (ft - pts0)
                            if lag < -1.0:  # salto de marcas de tiempo: nueva referencia
                                wall0, pts0 = now, ft
                            elif lag > MAX_LAG_SECONDS:
                                raise RuntimeError(f"retraso acumulado de {lag:.1f} s; se reconecta")
                    if now - last_conv < interval:
                        continue
                    size = self._target_size(frame.width, frame.height)
                    if size is None:
                        continue
                    last_conv = now
                    img = frame.to_ndarray(width=size[0], height=size[1], format="rgb24",
                                           interpolation="BILINEAR")
                    self._latest = (self._latest[0] + 1, img)
        finally:
            try:
                container.close()
            except Exception:
                pass


class LiveStreamManager:
    """Comparte un mismo flujo entre varias celdas que muestran la misma cámara."""

    def __init__(self, display_fps: int = 15):
        self.display_fps = display_fps
        self._streams: dict[tuple, tuple[LiveStream, int]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: tuple, url: str, label: str) -> LiveStream:
        with self._lock:
            entry = self._streams.get(key)
            if entry and not entry[0].stopped:
                self._streams[key] = (entry[0], entry[1] + 1)
                return entry[0]
            stream = LiveStream(url, label, self.display_fps).start()
            self._streams[key] = (stream, 1)
            return stream

    def release(self, stream: LiveStream) -> None:
        with self._lock:
            for key, (s, n) in list(self._streams.items()):
                if s is stream:
                    if n <= 1:
                        del self._streams[key]
                        s.stop()
                    else:
                        self._streams[key] = (s, n - 1)
                    return
        stream.stop()

    def streams_for(self, device_id: str) -> list[LiveStream]:
        with self._lock:
            return [s for k, (s, _) in self._streams.items() if k and k[0] == device_id]

    def active_count(self) -> int:
        with self._lock:
            return len(self._streams)

    def stop_all(self) -> None:
        with self._lock:
            streams = [s for s, _ in self._streams.values()]
            self._streams.clear()
        for s in streams:
            s.stop()
