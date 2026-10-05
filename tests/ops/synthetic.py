"""Imágenes SINTÉTICAS para probar la salud de imagen (CONTRATO §18.19).

Se generan en la propia prueba con formas geométricas (estanterías, cajas, rótulos, baldosas): nunca
fotos reales y nunca personas. Los «objetos que pasan» son elipses y rectángulos de color abstractos.
Todo es reproducible con una semilla.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import cv2
import numpy as np

W, H = 1280, 720   # «subflujo» de prueba; el análisis lo reduce a 640 px


def scene(seed: int = 1, *, w: int = W, h: int = H) -> np.ndarray:
    """Pasillo de tienda abstracto: pared, suelo con baldosas, estanterías con cajas y rótulos."""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.uint8)
    for y in range(h):   # pared con degradado y suelo
        if y < int(h * 0.62):
            v = 170 + int(40 * y / h)
            img[y, :] = (v - 10, v, v + 5)
        else:
            v = 120 + int(50 * (y - h * 0.62) / h)
            img[y, :] = (v, v - 5, v - 15)
    for x in range(0, w, int(rng.integers(70, 110))):   # baldosas
        cv2.line(img, (x, int(h * 0.62)), (int(x + (x - w / 2) * 0.4), h), (90, 90, 95), 2)
    for y in range(int(h * 0.66), h, 40):
        cv2.line(img, (0, y), (w, y), (95, 95, 100), 1)
    shelf_x = 20
    while shelf_x < w - 120:   # estanterías con cajas de colores
        sw = int(rng.integers(150, 260))
        top, bottom = int(h * 0.12), int(h * 0.62)
        cv2.rectangle(img, (shelf_x, top), (shelf_x + sw, bottom), (60, 70, 80), -1)
        for sy in np.linspace(top + 10, bottom - 10, 5).astype(int):
            cv2.line(img, (shelf_x, int(sy)), (shelf_x + sw, int(sy)), (200, 200, 205), 4)
            bx = shelf_x + 6
            while bx < shelf_x + sw - 20:
                bw = int(rng.integers(14, 34))
                bh = int(rng.integers(30, 60))
                color = tuple(int(c) for c in rng.integers(30, 240, 3))
                cv2.rectangle(img, (bx, int(sy) - bh), (bx + bw, int(sy) - 2), color, -1)
                cv2.rectangle(img, (bx, int(sy) - bh), (bx + bw, int(sy) - 2), (20, 20, 20), 1)
                if bw > 22:
                    cv2.line(img, (bx + 3, int(sy) - bh // 2), (bx + bw - 3, int(sy) - bh // 2), (250, 250, 250), 2)
                bx += bw + int(rng.integers(2, 6))
        shelf_x += sw + int(rng.integers(30, 70))
    for i in range(4):   # rótulos con texto
        x, y = int(rng.integers(40, w - 260)), int(rng.integers(10, int(h * 0.1)))
        cv2.rectangle(img, (x, y), (x + 220, y + 44), (30, 30, 160), -1)
        cv2.putText(img, f"OFERTA {i + 1}", (x + 12, y + 32), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    return img


def osd(img: np.ndarray, at: datetime) -> np.ndarray:
    """Reloj sobreimpreso (como el OSD de las cámaras): cambia aunque la imagen esté congelada."""
    out = img.copy()
    cv2.putText(out, at.strftime("%d-%m-%Y %H:%M:%S"), (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 2)
    return out


def jpeg(img: np.ndarray, quality: int = 85) -> np.ndarray:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    assert out is not None
    return out


def normal_frame(base: np.ndarray, i: int, *, start: datetime | None = None) -> np.ndarray:
    """Fotograma «normal»: ruido del sensor, leve variación de brillo y de posición (vibración de ±1 px),
    0-2 objetos abstractos que pasan, OSD con la hora y compresión JPEG."""
    rng = np.random.default_rng(1000 + i)
    h, w = base.shape[:2]
    img = base.astype(np.float32) * float(rng.uniform(0.96, 1.04))
    m = np.float32([[1, 0, rng.uniform(-1, 1)], [0, 1, rng.uniform(-1, 1)]])
    img = cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REFLECT)
    img += rng.normal(0, 2.0, img.shape).astype(np.float32)
    out = np.clip(img, 0, 255).astype(np.uint8)
    for _ in range(int(rng.integers(0, 3))):
        cx, cy = int(rng.integers(100, w - 100)), int(rng.integers(int(h * 0.4), h - 60))
        color = tuple(int(c) for c in rng.integers(20, 230, 3))
        if rng.random() < 0.5:
            cv2.ellipse(out, (cx, cy), (int(rng.integers(20, 45)), int(rng.integers(50, 110))), 0, 0, 360, color, -1)
        else:
            cv2.rectangle(out, (cx, cy), (cx + int(rng.integers(40, 90)), cy + int(rng.integers(30, 70))), color, -1)
    out = osd(out, (start or datetime(2026, 10, 5, 10, 0, 0)) + timedelta(seconds=7 * i))
    return jpeg(out)


def reference_frames(base: np.ndarray, n: int = 15) -> list[np.ndarray]:
    return [normal_frame(base, 500 + k) for k in range(n)]


def to_grey(img: np.ndarray, gain: float = 1.0) -> np.ndarray:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32) * gain
    g = np.clip(g, 0, 255).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def alterations(base: np.ndarray) -> dict[str, np.ndarray]:
    """Una alteración por causa sobre un fotograma normal (como la prueba de la investigación §3.2)."""
    frame = normal_frame(base, 60)
    h, w = frame.shape[:2]
    rng = np.random.default_rng(7)
    return {
        "black": jpeg((frame * 0.03).astype(np.uint8)),
        "covered": jpeg((np.full_like(frame, (40, 45, 50)) + rng.integers(0, 6, frame.shape, dtype=np.uint8))),
        "blurred": jpeg(cv2.GaussianBlur(frame, (0, 0), 6)),
        "moved": jpeg(cv2.warpAffine(frame, np.float32([[1, 0, 90], [0, 1, 35]]), (w, h),
                                     borderMode=cv2.BORDER_REPLICATE)),
        "rotated": jpeg(cv2.warpAffine(frame, cv2.getRotationMatrix2D((w / 2, h / 2), 9, 1.0), (w, h),
                                       borderMode=cv2.BORDER_REPLICATE)),
        # «Mira a otro sitio»: otro pasillo, visto desde el otro lado (la estructura no coincide)
        "scene_changed": cv2.flip(normal_frame(scene(seed=99), 61), 1),
        "degraded": jpeg(cv2.addWeighted(frame, 0.45, np.full_like(frame, 200), 0.55, 0)),
        "backlight": jpeg(cv2.convertScaleAbs(frame, alpha=2.6, beta=60)),
        "color_cast": jpeg(cv2.add(frame, np.full_like(frame, (0, 0, 70)))),
        "noise": jpeg(np.clip(frame.astype(np.float32) + rng.normal(0, 28, frame.shape), 0, 255).astype(np.uint8), 95),
        "ir_stuck": to_grey(cv2.convertScaleAbs(frame, alpha=1.0, beta=40)),
    }
