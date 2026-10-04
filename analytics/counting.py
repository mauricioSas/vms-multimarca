"""Seguimiento de personas y conteo: cruce de línea (puerta) y ocupación de zona (cola de cajas).

Conceptos, en sencillo:

- **Seguimiento (tracking)**: el detector ve personas en cada imagen, pero no sabe que la
  persona de esta imagen es la misma que la de la anterior. El seguidor ByteTrack (paquete
  `trackers`, Apache-2.0) une las cajas de imágenes consecutivas y les pone un número temporal
  (`tracker_id`). Ese número NO identifica a nadie: es un contador interno que se olvida a los
  pocos segundos de que la persona desaparezca. No se guarda en ningún sitio.

- **Línea de puerta**: una línea dibujada sobre la imagen. Si el pie de una persona (centro de
  la parte baja de su caja) pasa de un lado al otro, es un cruce: entrada o salida según el
  sentido (ver `LineRule` en vms/core/models.py). Usamos `supervision.LineZone` (MIT).

- **Anti-rebote (histéresis)**: alguien que se queda parado justo encima de la línea hace que su
  pie «tiemble» a uno y otro lado y se contaría varias veces. Lo evitamos de dos formas:
  1. Banda muerta: a cada lado de la línea hay una franja estrecha. Dentro de ella, para el
     conteo se considera que la persona sigue en el lado donde estaba. Solo cuenta cuando sale
     claramente por el otro lado.
  2. Confirmación temporal: tiene que verse al otro lado durante un mínimo de imágenes seguidas
     (`minimum_crossing_threshold` de LineZone).
  Además, si el detector pierde a una persona un instante (alguien la tapa), se recuerda su
  última posición durante ~1,5 s para que el cruce no se pierda.

- **Zona de cola**: un polígono sobre la zona de cajas. La ocupación es cuántas personas tienen
  el pie dentro. Usamos `supervision.PolygonZone` (MIT).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

from vms.core.models import LineRule, ZoneRule


# =========================================================================== seguimiento
def make_tracker(fps: float, confidence: float, memory_seconds: float = 1.5) -> ByteTrackTracker:
    """Crea un ByteTrack ajustado a la frecuencia de análisis de la cámara.

    - `lost_track_buffer` se expresa en «imágenes a 30 fps»: 1,5 s = 45.
    - El detector entrega también detecciones algo por debajo del umbral (hasta la mitad): ByteTrack
      las usa solo para no perder a alguien ya seguido (por ejemplo, medio tapado), nunca para
      crear una persona nueva. Las personas nuevas exigen la confianza configurada.
    - `minimum_consecutive_frames=2`: una caja suelta de una sola imagen (un falso positivo) no
      llega a convertirse en persona seguida. A 1-2 fps (cajas) se usa 1 para no retrasar el conteo.
    """
    return ByteTrackTracker(
        lost_track_buffer=max(1, int(round(memory_seconds * 30))),
        frame_rate=float(fps),
        track_activation_threshold=confidence,
        high_conf_det_threshold=confidence,
        minimum_consecutive_frames=2 if fps >= 4 else 1,
        minimum_iou_threshold=0.1,
    )


def detection_floor(confidence: float) -> float:
    """Umbral mínimo que se pide al detector (las débiles solo sirven para mantener el seguimiento)."""
    return max(0.1, confidence * 0.5)


def bottom_centers(xyxy: np.ndarray) -> np.ndarray:
    """Punto de referencia de cada caja: el centro de su borde inferior (los pies)."""
    return np.stack([(xyxy[:, 0] + xyxy[:, 2]) / 2.0, xyxy[:, 3]], axis=1) if len(xyxy) else np.empty((0, 2))


# =========================================================================== línea
@dataclass
class _TrackMemory:
    point: tuple[float, float]   # posición «efectiva» (con banda muerta aplicada)
    last_seen: float


class LineCounter:
    """Cuenta entradas y salidas por una línea, con anti-rebote. Ver docstring del módulo."""

    def __init__(self, rule: LineRule, frame_size: tuple[int, int], *, fps: float,
                 hysteresis: float = 0.02, min_seconds: float = 0.15, memory_seconds: float = 1.5) -> None:
        w, h = frame_size
        self.rule_id = rule.id
        self.camera_id = rule.camera_id
        self.invert = rule.invert
        self._start = (rule.start[0] * w, rule.start[1] * h)
        self._end = (rule.end[0] * w, rule.end[1] * h)
        vx, vy = self._end[0] - self._start[0], self._end[1] - self._start[1]
        self._length = math.hypot(vx, vy)
        self._margin = hysteresis * math.hypot(w, h)
        self._memory_s = memory_seconds
        self.min_frames = max(1, math.ceil(min_seconds * fps - 1e-9))
        # Sentido: supervision cuenta «in» cuando el punto pasa al lado donde su producto
        # vectorial con (inicio→fin) es negativo. Nuestra ENTRADA es pasar a s(p) > 0 con
        # s = producto vectorial respecto a (start→end). Si le damos a supervision la línea al
        # revés (end→start), su producto vectorial es -s y su «in» coincide con nuestra entrada.
        self._zone = sv.LineZone(start=sv.Point(*self._end), end=sv.Point(*self._start),
                                 triggering_anchors=[sv.Position.CENTER],
                                 minimum_crossing_threshold=self.min_frames)
        self._tracks: dict[int, _TrackMemory] = {}
        self.total_in = 0
        self.total_out = 0

    def signed_distance(self, p: tuple[float, float]) -> float:
        """s(p)/|v| en píxeles: > 0 lado de «dentro», < 0 lado de «fuera» (sin invertir)."""
        vx, vy = self._end[0] - self._start[0], self._end[1] - self._start[1]
        s = vx * (p[1] - self._start[1]) - vy * (p[0] - self._start[0])
        return s / self._length

    def update(self, tracked: sv.Detections, now: float) -> tuple[int, int]:
        """Procesa las personas seguidas de una imagen. Devuelve (entradas, salidas) nuevas."""
        ids: list[int] = []
        points: list[tuple[float, float]] = []
        seen: set[int] = set()
        if len(tracked) and tracked.tracker_id is not None:
            feet = bottom_centers(tracked.xyxy)
            for tid, (x, y) in zip(tracked.tracker_id, feet, strict=True):
                tid = int(tid)
                if tid < 0 or tid in seen:
                    continue   # aún sin confirmar por el seguidor
                seen.add(tid)
                raw = (float(x), float(y))
                mem = self._tracks.get(tid)
                if mem is not None and abs(self.signed_distance(raw)) < self._margin:
                    effective = mem.point          # dentro de la banda muerta: no cambia de lado
                else:
                    effective = raw
                self._tracks[tid] = _TrackMemory(effective, now)
                ids.append(tid)
                points.append(effective)
        # Personas perdidas un instante: se mantienen en su última posición efectiva.
        for tid, mem in list(self._tracks.items()):
            if tid in seen:
                continue
            if now - mem.last_seen > self._memory_s:
                del self._tracks[tid]
                continue
            ids.append(tid)
            points.append(mem.point)
        if ids:
            xy = np.asarray(points, dtype=np.float32)
            det = sv.Detections(xyxy=np.concatenate([xy, xy], axis=1), tracker_id=np.asarray(ids, dtype=int),
                                class_id=np.zeros(len(ids), dtype=int))
        else:
            det = sv.Detections.empty()
            det.tracker_id = np.empty(0, dtype=int)
        crossed_in, crossed_out = self._zone.trigger(det)
        n_in, n_out = int(np.count_nonzero(crossed_in)), int(np.count_nonzero(crossed_out))
        if self.invert:
            n_in, n_out = n_out, n_in
        self.total_in += n_in
        self.total_out += n_out
        return n_in, n_out


# =========================================================================== zona
class ZoneCounter:
    """Ocupación de una zona: personas con el pie dentro del polígono."""

    def __init__(self, rule: ZoneRule, frame_size: tuple[int, int]) -> None:
        w, h = frame_size
        self.rule_id = rule.id
        self.camera_id = rule.camera_id
        poly = np.array([[round(x * (w - 1)), round(y * (h - 1))] for x, y in rule.polygon], dtype=np.int64)
        self._zone = sv.PolygonZone(polygon=poly, triggering_anchors=(sv.Position.BOTTOM_CENTER,))

    def count(self, tracked: sv.Detections, confidence: float) -> int:
        """Cuenta personas seguidas (id >= 0) y también las recién aparecidas con confianza alta.

        A 1-2 imágenes por segundo, el seguidor tarda una imagen en confirmar a alguien nuevo; para
        la ocupación no hace falta esperar: una detección segura ya es una persona presente.
        """
        if len(tracked) == 0:
            return 0
        if tracked.tracker_id is not None:
            conf = tracked.confidence if tracked.confidence is not None else np.ones(len(tracked))
            keep = (tracked.tracker_id >= 0) | (conf >= confidence)
            tracked = tracked[keep]
            if len(tracked) == 0:
                return 0
        inside = self._zone.trigger(tracked)
        return int(np.count_nonzero(inside))
