"""Proceso de una cámara: vídeo → detección → seguimiento → línea/zona → minuto y alertas.

Cada cámara con analítica tiene su propio hilo (CameraPipeline) más el hilo lector de vídeo
(FrameGrabber). El hilo procesa a la frecuencia configurada (`fps`): puerta 10-15 imágenes por
segundo para no perder a quien cruza deprisa; cajas 1-2 por segundo, porque la cola cambia
despacio. Las imágenes que llegan entre medias se descartan sin analizar.

La inferencia (OpenVINO / ONNX Runtime) libera el GIL de Python, así que varias cámaras se
procesan realmente en paralelo en los distintos núcleos de la CPU.

RGPD: la imagen se usa en memoria y se descarta al terminar cada vuelta del bucle.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import supervision as sv

from vms.core.models import LineRule, ZoneRule

from .aggregation import MinuteAggregator
from .alerts import AlertEvent, QueueAlertMonitor
from .config import CameraJob
from .counting import LineCounter, ZoneCounter, detection_floor, make_tracker
from .detector import DetectorRegistry, DetectorUnavailable, LatencyStats, PersonDetector
from .settings import AnalyticsSettings
from .video import Frame, FrameGrabber, RateMeter

log = logging.getLogger("analytics.pipeline")

DETECTOR_RETRY_SECONDS = 30.0


def _rule_geometry_key(rule: LineRule | ZoneRule) -> str:
    """Lo que obliga a recrear el contador (cambios de nombre o umbral no lo reinician)."""
    if isinstance(rule, LineRule):
        return f"line|{rule.start}|{rule.end}|{rule.invert}"
    return f"zone|{rule.polygon}"


class CameraPipeline:
    def __init__(self, job: CameraJob, registry: DetectorRegistry, aggregator: MinuteAggregator,
                 settings: AnalyticsSettings, on_alert: Callable[[AlertEvent], None], *,
                 grabber_factory: Callable[[str, str], FrameGrabber] | None = None) -> None:
        self.job = job
        self.camera_id = job.camera_id
        self._registry = registry
        self._aggregator = aggregator
        self._settings = settings
        self._on_alert = on_alert
        self._grabber_factory = grabber_factory or (lambda url, name: FrameGrabber(url, name))
        self._grabber: FrameGrabber | None = None
        self._detector: PersonDetector | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rules_lock = threading.Lock()
        self._rules: list[LineRule | ZoneRule] = job.active_rules()
        self._rules_dirty = True
        self._frame_size: tuple[int, int] | None = None
        self._lines: dict[str, LineCounter] = {}
        self._zones: dict[str, ZoneCounter] = {}
        self._monitors: dict[str, QueueAlertMonitor] = {}
        self._geometry: dict[str, str] = {}
        self._tracker: Any = None
        self._last_zone_sample: float | None = None
        # métricas
        self.latency = LatencyStats()
        self.processed = RateMeter()
        self.frames_processed = 0
        self.last_error = ""
        self.detector_error = ""
        self.occupancy: dict[str, int] = {}
        self.totals: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> "CameraPipeline":
        self._grabber = self._grabber_factory(self.job.rtsp_url, self.job.name or self.camera_id).start()
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.camera_id}", daemon=True)
        self._thread.start()
        log.info("[%s] analítica iniciada (%.1f fps, %s, %d reglas)", self.camera_id, self.job.fps,
                 self.job.detector, len(self._rules))
        return self

    def stop(self, timeout: float = 15.0) -> list[AlertEvent]:
        """Para la cámara. Devuelve las alertas que quedaron abiertas, ya cerradas."""
        self._stop.set()
        if self._grabber is not None:
            self._grabber.stop(timeout)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("[%s] el hilo de análisis no terminó a tiempo", self.camera_id)
        now = datetime.now(timezone.utc)
        closed = [ev for m in self._monitors.values() if (ev := m.close(now)) is not None]
        log.info("[%s] analítica detenida", self.camera_id)
        return closed

    def set_rules(self, rules: list[LineRule | ZoneRule]) -> None:
        """Cambia las reglas en caliente (sin cortar el vídeo)."""
        with self._rules_lock:
            self._rules = [r for r in rules if r.enabled]
            self._rules_dirty = True

    # ------------------------------------------------------------------ estado
    def status(self) -> dict[str, Any]:
        g = self._grabber
        if self.detector_error:
            state, err = "error", self.detector_error
        elif g is None or g.state in ("connecting",):
            state, err = "connecting", (g.last_error if g else "")
        elif g.state == "error":
            state, err = "error", g.last_error
        else:
            state, err = "running", self.last_error
        p50 = self.latency.percentile(50)
        p95 = self.latency.percentile(95)
        return {
            "camera_id": self.camera_id,
            "name": self.job.name,
            "state": state,
            "fps_target": self.job.fps,
            "fps_in": round(g.input_rate.rate(), 2) if g else 0.0,
            "fps_processed": round(self.processed.rate(), 2),
            "inference_ms_p50": round(p50, 1) if p50 is not None else None,
            "inference_ms_p95": round(p95, 1) if p95 is not None else None,
            "frames_processed": self.frames_processed,
            "reconnects": g.reconnects if g else 0,
            "detector": {"model": self.job.detector,
                         "backend": getattr(self._detector, "backend", None)},
            "frame_size": list(self._frame_size) if self._frame_size else None,
            "last_error": err,
            "occupancy": dict(self.occupancy),
            "totals": {k: dict(v) for k, v in self.totals.items()},
        }

    # ------------------------------------------------------------------ hilo
    def _load_detector(self) -> bool:
        try:
            self._detector = self._registry.get(self.job.detector)
            self.detector_error = ""
            return True
        except DetectorUnavailable as exc:
            if self.detector_error != str(exc):
                log.error("[%s] %s", self.camera_id, exc)
            self.detector_error = str(exc)
            return False

    def _run(self) -> None:
        assert self._grabber is not None
        while not self._stop.is_set() and not self._load_detector():
            self._stop.wait(DETECTOR_RETRY_SECONDS)
        period = 1.0 / self.job.fps
        next_t = time.monotonic()
        last_seq = 0
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                self._stop.wait(next_t - now)
                continue
            frame = self._grabber.wait_newer(last_seq, timeout=2.0)
            if frame is None:
                continue
            last_seq = frame.seq
            next_t += period
            if next_t < time.monotonic():
                next_t = time.monotonic()   # vamos tarde: no se acumulan vueltas pendientes
            try:
                self._process(frame)
                self.last_error = ""
            except Exception as exc:  # un fallo en una imagen no debe parar la cámara
                if self.last_error != str(exc):
                    log.exception("[%s] error al analizar una imagen", self.camera_id)
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                del frame   # RGPD: la imagen no se conserva

    def _rebuild(self, size: tuple[int, int]) -> None:
        with self._rules_lock:
            rules = list(self._rules)
            self._rules_dirty = False
        resized = size != self._frame_size
        if resized or self._tracker is None:
            self._tracker = make_tracker(self.job.fps, self.job.confidence, self._settings.track_memory_seconds)
            if self._frame_size is not None:
                log.info("[%s] resolución del flujo: %dx%d", self.camera_id, *size)
        self._frame_size = size
        s = self._settings
        wanted = {r.id for r in rules}
        for rid in list(self._lines) + list(self._zones):
            if rid not in wanted:
                self._lines.pop(rid, None)
                self._zones.pop(rid, None)
                self._geometry.pop(rid, None)
                self.occupancy.pop(rid, None)
                mon = self._monitors.pop(rid, None)
                if mon is not None and (ev := mon.close(datetime.now(timezone.utc))) is not None:
                    self._on_alert(ev)
        for r in rules:
            key = _rule_geometry_key(r)
            fresh = resized or self._geometry.get(r.id) != key
            if isinstance(r, LineRule):
                if fresh or r.id not in self._lines:
                    self._lines[r.id] = LineCounter(r, size, fps=self.job.fps, hysteresis=s.line_hysteresis,
                                                    min_seconds=s.line_min_seconds,
                                                    memory_seconds=s.track_memory_seconds)
                else:
                    self._lines[r.id].invert = r.invert
                self.totals.setdefault(r.id, {"in": 0, "out": 0})
            else:
                if fresh or r.id not in self._zones:
                    self._zones[r.id] = ZoneCounter(r, size)
                mon = self._monitors.get(r.id)
                if mon is None:
                    self._monitors[r.id] = QueueAlertMonitor(r, clear_seconds=s.alert_clear_seconds)
                else:
                    mon.rule = r   # umbrales nuevos sin perder el estado de la alerta en curso
            self._geometry[r.id] = key

    def _process(self, frame: Frame) -> None:
        assert self._detector is not None
        h, w = frame.image.shape[:2]
        if self._rules_dirty or (w, h) != self._frame_size:
            self._rebuild((w, h))
        t0 = time.perf_counter()
        detections: sv.Detections = self._detector.detect(frame.image, detection_floor(self.job.confidence))
        self.latency.add((time.perf_counter() - t0) * 1000.0)
        tracked = self._tracker.update(detections, timestamp=frame.mono_time)
        wall_dt = datetime.fromtimestamp(frame.wall_time, tz=timezone.utc)
        for rid, lc in self._lines.items():
            n_in, n_out = lc.update(tracked, frame.mono_time)
            self._aggregator.add_line(rid, self.camera_id, frame.wall_time, n_in, n_out)
            if n_in or n_out:
                t = self.totals.setdefault(rid, {"in": 0, "out": 0})
                t["in"] += n_in
                t["out"] += n_out
                log.debug("[%s] %s: +%d entradas, +%d salidas", self.camera_id, rid, n_in, n_out)
        if self._zones:
            prev = self._last_zone_sample
            dt = 1.0 / self.job.fps if prev is None else min(frame.mono_time - prev, 2.0 / self.job.fps)
            self._last_zone_sample = frame.mono_time
            for rid, zc in self._zones.items():
                occ = zc.count(tracked, self.job.confidence)
                self.occupancy[rid] = occ
                mon = self._monitors[rid]
                self._aggregator.add_zone(rid, self.camera_id, frame.wall_time, occ, occ >= mon.threshold, dt)
                for ev in mon.update(occ, frame.mono_time, wall_dt):
                    self._on_alert(ev)
        self.frames_processed += 1
        self.processed.tick(time.monotonic())
