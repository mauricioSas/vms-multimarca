"""Utilidades para generar detecciones sintéticas (personas que se mueven) en las pruebas."""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import supervision as sv

W, H = 1000, 1000


def box_at(foot_x: float, foot_y: float, w: float = 40, h: float = 100) -> list[float]:
    """Caja cuya «referencia» (centro del borde inferior) está en (foot_x, foot_y)."""
    return [foot_x - w / 2, foot_y - h, foot_x + w / 2, foot_y]


def tracked(points: Iterable[tuple[int, float, float]]) -> sv.Detections:
    """Detecciones ya seguidas: [(tracker_id, pie_x, pie_y), ...]."""
    pts = list(points)
    if not pts:
        d = sv.Detections.empty()
        d.tracker_id = np.empty(0, dtype=int)
        return d
    return sv.Detections(xyxy=np.array([box_at(x, y) for _, x, y in pts], dtype=np.float32),
                         confidence=np.full(len(pts), 0.9, dtype=np.float32),
                         class_id=np.zeros(len(pts), dtype=int),
                         tracker_id=np.array([t for t, _, _ in pts], dtype=int))


def raw(points: Iterable[tuple[float, float]], conf: float = 0.9) -> sv.Detections:
    """Detecciones sin seguir (lo que devuelve el detector)."""
    pts = list(points)
    if not pts:
        return sv.Detections(xyxy=np.empty((0, 4), dtype=np.float32), confidence=np.empty(0, dtype=np.float32),
                             class_id=np.empty(0, dtype=int))
    return sv.Detections(xyxy=np.array([box_at(x, y) for x, y in pts], dtype=np.float32),
                         confidence=np.full(len(pts), conf, dtype=np.float32),
                         class_id=np.zeros(len(pts), dtype=int))


def path(y0: float, y1: float, steps: int) -> list[float]:
    return list(np.linspace(y0, y1, steps))
