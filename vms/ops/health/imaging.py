"""Salud de imagen con visión clásica (OpenCV + NumPy, sin modelos): CONTRATO §18.2.

Idea: casi todo se detecta **comparando con una referencia de la misma cámara** (no con umbrales
absolutos), porque cada escena es distinta. Hay dos referencias por cámara, día y noche (IR); cada una
es la **mediana** de unos 15 fotogramas tomados durante unos minutos, que borra a quien pasa por delante.

Todo trabaja a `WORK_WIDTH` píxeles de ancho (640) para que los umbrales no dependan de la resolución
y para cumplir el objetivo de < 20 ms por comprobación. Las imágenes solo viven en memoria: aquí no se
escribe nada en disco (la referencia la guarda `references.py`).

Lecciones del prototipo (investigación §3.2) que se aplican:
1. ORB + `estimateAffinePartial2D` da desplazamientos falsos con la imagen desenfocada o tapada: «movida
   o girada» solo cuenta si la proporción de puntos coincidentes (inliers) supera `MIN_INLIERS` (15 %).
   Si no, la causa es otra (desenfoque, tapada, mira a otro sitio).
2. La dominante de color sintética sale floja en escenas muy saturadas: el umbral es relativo a la
   referencia y hay que calibrarlo con cámaras reales en el piloto.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

import cv2
import numpy as np
import numpy.typing as npt

from ..models import HealthCause, HealthCheck, HealthMetrics

Image = npt.NDArray[Any]       # BGR o gris, uint8 (OpenCV no tipa el dtype de forma útil)
RefKind = Literal["day", "night"]

WORK_WIDTH = 640
ORB_FEATURES = 600
MIN_INLIERS = 0.15            # puerta de calidad de la geometría (lección 1)
SCENE_CHANGED_INLIERS = 0.06  # por debajo, con imagen nítida y bordes perdidos: mira a otro sitio
GREY_SATURATION = 8.0         # saturación media (0-255) por debajo de la cual la imagen es «gris» (IR)
FROZEN_DIFF = 0.25            # diferencia media entre comprobaciones por debajo de la cual está congelada
OSD_BANDS = (0.10, 0.08)      # franjas superior e inferior que se ignoran al buscar «congelada» (reloj del OSD)

# Penalizaciones (CONTRATO §18.2): tapada/negra/congelada = 0; movida −40 a −60; desenfoque −10 a −40;
# color/ruido/contraluz −5 a −15.
HARD_ZERO = (HealthCause.BLACK, HealthCause.COVERED, HealthCause.FROZEN)
CAUSE_ORDER = [HealthCause.BLACK, HealthCause.COVERED, HealthCause.FROZEN, HealthCause.SCENE_CHANGED,
               HealthCause.MOVED, HealthCause.ROTATED, HealthCause.IR_STUCK, HealthCause.IR_WEAK,
               HealthCause.BLURRED, HealthCause.DEGRADED, HealthCause.BACKLIGHT, HealthCause.COLOR_CAST,
               HealthCause.NOISE]


def status_for(score: int | None) -> Literal["ok", "warning", "critical", "unknown"]:
    if score is None:
        return "unknown"
    if score >= 80:
        return "ok"
    if score >= 50:
        return "warning"
    return "critical"


# --------------------------------------------------------------------------- utilidades de imagen
def decode_image(data: bytes) -> Image | None:
    """JPEG/PNG en memoria → BGR. None si no se puede decodificar."""
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img if img is not None and img.size else None


def encode_jpeg(img: Image, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("No se pudo codificar la imagen")
    return bytes(buf)


def to_work(img: Image) -> Image:
    """Escala a `WORK_WIDTH` de ancho conservando la proporción (siempre la misma escala de trabajo)."""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    h, w = img.shape[:2]
    if w == WORK_WIDTH:
        return img
    nh = max(1, int(round(h * WORK_WIDTH / w)))
    interp = cv2.INTER_AREA if w > WORK_WIDTH else cv2.INTER_LINEAR
    out: Image = cv2.resize(img, (WORK_WIDTH, nh), interpolation=interp)
    return out


def mask_from_polygons(shape: tuple[int, int], polygons: list[list[tuple[float, float]]]) -> Image:
    """Máscara 255 = se compara, 0 = zona excluida (puertas automáticas, pantallas, OSD)."""
    h, w = shape
    mask = np.full((h, w), 255, dtype=np.uint8)
    for poly in polygons:
        if len(poly) < 3:
            continue
        pts = np.array([[int(round(x * (w - 1))), int(round(y * (h - 1)))] for x, y in poly], dtype=np.int32)
        cv2.fillPoly(mask, [pts], 0)
    return mask


def _osd_mask(mask: Image) -> Image:
    h = mask.shape[0]
    out = mask.copy()
    top, bottom = int(h * OSD_BANDS[0]), int(h * OSD_BANDS[1])
    out[:top, :] = 0
    if bottom:
        out[h - bottom:, :] = 0
    return out


def _lap_var(gray: Image, mask: Image) -> float:
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    vals = lap[mask > 0]
    return float(vals.var()) if vals.size else 0.0


def _edges(gray: Image) -> Image:
    out: Image = cv2.Canny(gray, 60, 150)
    return out


def _entropy(gray: Image, mask: Image) -> float:
    hist = cv2.calcHist([gray], [0], mask, [64], [0, 256]).ravel()
    total = hist.sum()
    if total <= 0:
        return 0.0
    p = hist[hist > 0] / total
    return float(-(p * np.log2(p)).sum())


def _noise_sigma(gray: Image, flat: Image) -> float:
    """Ruido en zonas planas: mediana del residuo frente a un filtro de mediana (estimador robusto)."""
    resid = cv2.absdiff(gray, cv2.medianBlur(gray, 3))
    vals = resid[flat > 0]
    if vals.size < 500:
        vals = resid.ravel()
    return float(np.median(vals) * 1.4826)


# --------------------------------------------------------------------------- referencia
@dataclass
class Reference:
    """Imagen de referencia ya preparada para comparar (se calcula una vez y se reutiliza)."""

    kind: RefKind
    image: Image
    mask: Image
    gray: Image = field(init=False)
    sharpness: float = field(init=False)
    edge_count: int = field(init=False)
    edges_dilated: Image = field(init=False)
    keypoints: tuple[cv2.KeyPoint, ...] = field(init=False)
    descriptors: Image | None = field(init=False)
    lab_ab: tuple[float, float] = field(init=False)
    std: float = field(init=False)
    luma: float = field(init=False)
    saturation: float = field(init=False)
    noise: float = field(init=False)
    flat: Image = field(init=False)

    def __post_init__(self) -> None:
        self.gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        m = self.mask
        self.sharpness = max(_lap_var(self.gray, m), 1e-6)
        edges = cv2.bitwise_and(_edges(self.gray), m)
        self.edge_count = int(np.count_nonzero(edges))
        self.edges_dilated = cv2.dilate(edges, np.ones((5, 5), np.uint8))
        orb = cv2.ORB.create(ORB_FEATURES)
        kp, desc = orb.detectAndCompute(self.gray, m)
        self.keypoints = tuple(kp)
        self.descriptors = desc
        lab = cv2.cvtColor(self.image, cv2.COLOR_BGR2LAB)
        self.lab_ab = (float(lab[..., 1][m > 0].mean()), float(lab[..., 2][m > 0].mean()))
        vals = self.gray[m > 0]
        self.std = float(vals.std()) if vals.size else 0.0
        self.luma = float(vals.mean()) if vals.size else 0.0
        hsv = cv2.cvtColor(self.image, cv2.COLOR_BGR2HSV)
        self.saturation = float(hsv[..., 1][m > 0].mean())
        self.flat = cv2.bitwise_and(cv2.bitwise_not(cv2.dilate(edges, np.ones((9, 9), np.uint8))), m)
        self.noise = max(_noise_sigma(self.gray, self.flat), 0.5)

    @classmethod
    def from_image(cls, image: Image, kind: RefKind,
                   masks: list[list[tuple[float, float]]] | None = None) -> "Reference":
        work = to_work(image)
        return cls(kind=kind, image=work, mask=mask_from_polygons(work.shape[:2], masks or []))


@dataclass
class ReferenceBuild:
    image: Image
    frames: int
    activity_ratio: float          # % de la imagen con movimiento mientras se fijaba
    person_warning: bool           # hubo movimiento: puede quedar alguien quieto en la referencia


def build_reference(frames: list[Image], *, activity_threshold: float = 0.02) -> ReferenceBuild:
    """Mediana de los fotogramas (borra a quien pasa). Avisa si hubo mucho movimiento durante la toma.

    No detecta personas (RGPD): solo mide cuánta parte de la imagen cambió entre fotogramas. Si fue
    mucha, recomienda repetir con la tienda cerrada, porque alguien quieto todo el rato no se borra."""
    work = [to_work(f) for f in frames if f is not None and f.size]
    if len(work) < 3:
        raise ValueError("Hacen falta al menos 3 fotogramas para fijar la referencia")
    h, w = work[0].shape[:2]
    work = [f if f.shape[:2] == (h, w) else cv2.resize(f, (w, h)) for f in work]
    stack = np.stack(work)
    median: Image = np.median(stack, axis=0).astype(np.uint8)
    # Se compara con la imagen suavizada para que la vibración de ±1 px en los bordes no cuente como movimiento.
    gmed = cv2.GaussianBlur(cv2.cvtColor(median, cv2.COLOR_BGR2GRAY), (7, 7), 0).astype(np.int16)
    moving = np.zeros((h, w), dtype=np.float32)
    for f in work:
        g = cv2.GaussianBlur(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (7, 7), 0).astype(np.int16)
        moving += (np.abs(g - gmed) > 30).astype(np.float32)
    activity = float(((moving / len(work)) > 0.3).mean())
    return ReferenceBuild(image=median, frames=len(work), activity_ratio=round(activity, 4),
                          person_warning=activity > activity_threshold)


# --------------------------------------------------------------------------- análisis
@dataclass
class Analysis:
    metrics: HealthMetrics
    causes: list[HealthCause]
    penalties: dict[HealthCause, int]
    score: int | None
    reference: Literal["day", "night", "none"]
    duration_ms: float
    is_grey: bool


def choose_reference(frame: Image, day: Reference | None, night: Reference | None) -> Reference | None:
    """Gris (IR) → referencia de noche si existe; color → la de día. Si solo hay una, esa."""
    if day is None and night is None:
        return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    grey = float(hsv[..., 1].mean()) < GREY_SATURATION
    if grey and night is not None:
        return night
    return day if day is not None else night


def _geometry(gray: Image, ref: Reference, mask: Image) -> tuple[float, float, float, float]:
    """(inliers relativos a la referencia, desplazamiento px, giro en grados, escala)."""
    if ref.descriptors is None or len(ref.keypoints) < 10:
        return 0.0, 0.0, 0.0, 1.0
    orb = cv2.ORB.create(ORB_FEATURES)
    kp, desc = orb.detectAndCompute(gray, mask)
    if desc is None or len(kp) < 10:
        return 0.0, 0.0, 0.0, 1.0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = matcher.match(ref.descriptors, desc)
    if len(matches) < 8:
        return 0.0, 0.0, 0.0, 1.0
    p1 = np.array([ref.keypoints[m.queryIdx].pt for m in matches], dtype=np.float32)
    p2 = np.array([kp[m.trainIdx].pt for m in matches], dtype=np.float32)
    affine, inl = cv2.estimateAffinePartial2D(p1, p2, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if affine is None or inl is None:
        return 0.0, 0.0, 0.0, 1.0
    ratio = float(inl.sum()) / max(1, len(ref.keypoints))
    # El desplazamiento se mide en el centro de la imagen: un giro puro no cuenta como «movida».
    h, w = gray.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    nx = affine[0, 0] * cx + affine[0, 1] * cy + affine[0, 2]
    ny = affine[1, 0] * cx + affine[1, 1] * cy + affine[1, 2]
    shift = float(np.hypot(nx - cx, ny - cy))
    rot = float(np.degrees(np.arctan2(affine[1, 0], affine[0, 0])))
    scale = float(np.hypot(affine[0, 0], affine[1, 0]))
    return ratio, shift, rot, scale


def analyze(frame_bgr: Image, *, day: Reference | None, night: Reference | None,
            previous: Image | None = None, masks: list[list[tuple[float, float]]] | None = None) -> Analysis:
    """Mide un fotograma frente a su referencia y devuelve medidas, causas y puntuación 0-100.

    `previous` = fotograma de la comprobación anterior (en memoria), para detectar «congelada»."""
    t0 = time.perf_counter()
    frame = to_work(frame_bgr)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    ref = choose_reference(frame, day, night)
    mask = ref.mask if ref is not None and ref.mask.shape == gray.shape else mask_from_polygons((h, w), masks or [])
    vals = gray[mask > 0]
    if vals.size == 0:
        vals = gray.ravel()
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    sat = float(hsv[..., 1][mask > 0].mean()) if np.count_nonzero(mask) else float(hsv[..., 1].mean())
    m = HealthMetrics(
        luma_mean=round(float(vals.mean()), 2),
        dark_ratio=round(float((vals < 8).mean()), 4),
        bright_ratio=round(float((vals > 250).mean()), 4),
        std=round(float(vals.std()), 2),
        entropy=round(_entropy(gray, mask), 3),
        saturation_mean=round(sat, 2),
    )
    is_grey = sat < GREY_SATURATION
    if previous is not None:
        prev = to_work(previous)
        if prev.shape == frame.shape:
            osd = _osd_mask(mask)
            diff = cv2.absdiff(cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY), gray)
            sel = diff[osd > 0]
            m.frame_diff = round(float(sel.mean()) if sel.size else float(diff.mean()), 3)

    if ref is None:
        return Analysis(metrics=m, causes=[HealthCause.NO_REFERENCE], penalties={}, score=None, reference="none",
                        duration_ms=round((time.perf_counter() - t0) * 1000, 2), is_grey=is_grey)

    edges = cv2.bitwise_and(_edges(gray), mask)
    m.sharpness_rel = round(_lap_var(gray, mask) / ref.sharpness, 3)
    m.edges_kept = round(float(np.count_nonzero(cv2.bitwise_and(edges, ref.edges_dilated)))
                         / max(1, ref.edge_count), 3)
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    a, b = float(lab[..., 1][mask > 0].mean()), float(lab[..., 2][mask > 0].mean())
    m.color_delta_ab = round(float(np.hypot(a - ref.lab_ab[0], b - ref.lab_ab[1])), 2)

    penalties: dict[HealthCause, int] = {}
    luma, std = float(m.luma_mean or 0), float(m.std or 0)
    black = luma < 12 and (m.dark_ratio or 0) > 0.80
    covered = (not black and std < 10 and (m.entropy or 0) < 2.5 and (m.edges_kept or 0) < 0.10)
    frozen = m.frame_diff is not None and m.frame_diff < FROZEN_DIFF and std > 3
    if black:
        penalties[HealthCause.BLACK] = 100
    elif covered:
        penalties[HealthCause.COVERED] = 100
    elif frozen:
        penalties[HealthCause.FROZEN] = 100
    else:
        inl, shift, rot, scale = _geometry(gray, ref, mask)
        m.inliers_ratio, m.shift_px, m.rotation_deg = round(inl, 3), round(shift, 1), round(rot, 2)
        sharp_rel, kept = float(m.sharpness_rel or 0), float(m.edges_kept or 0)
        haze = std < 0.7 * ref.std and luma > ref.luma + 12
        shift_thr = max(15.0, 0.03 * w)
        if inl >= MIN_INLIERS:
            if shift > shift_thr:
                penalties[HealthCause.MOVED] = int(round(40 + 20 * min(1.0, (shift - shift_thr) / 80.0)))
            if abs(rot) > 4.0:
                penalties[HealthCause.ROTATED] = int(round(40 + 20 * min(1.0, (abs(rot) - 4.0) / 16.0)))
        bright, dark = float(m.bright_ratio or 0), float(m.dark_ratio or 0)
        backlight = bright > 0.12 or (bright > 0.05 and dark > 0.05)
        # Casi ningún punto coincide, pero la imagen es nítida y con contraste: no está tapada ni borrosa,
        # mira a otro sitio (con pocos inliers nunca se informa de un desplazamiento: lección 1).
        scene_lost = (inl < SCENE_CHANGED_INLIERS and sharp_rel > 0.5 and kept < 0.75 and std > 0.6 * ref.std
                      and not haze and not backlight)
        if scene_lost:
            penalties[HealthCause.SCENE_CHANGED] = 60
        moved = bool(penalties)
        # IR: gris y muy luminosa = filtro atascado en modo noche con luz de día; color y oscura = no pasa a
        # modo noche; gris y mucho más oscura que la referencia de noche = LED IR fallando.
        ir_stuck_dark = False
        if (is_grey and luma > 140 and day is not None and day.saturation > 20
                and (night is None or luma > 1.4 * night.luma)):
            penalties[HealthCause.IR_STUCK] = 30
        elif not is_grey and luma < 40 and sat > 25 and night is not None:
            penalties[HealthCause.IR_STUCK] = 30
            ir_stuck_dark = True            # la oscuridad explica la falta de nitidez: no se suma «desenfocada»
        elif is_grey and ref.kind == "night" and luma < 0.55 * ref.luma:
            penalties[HealthCause.IR_WEAK] = 25
        if backlight:
            penalties[HealthCause.BACKLIGHT] = int(round(10 + 5 * min(1.0, (bright - 0.05) / 0.3)))
        elif not moved and not ir_stuck_dark:
            if haze and sharp_rel < 0.85:
                penalties[HealthCause.DEGRADED] = int(round(15 + 15 * min(1.0, (0.7 * ref.std - std) / (0.5 * ref.std))))
            elif sharp_rel < 0.45 and kept < 0.6:
                penalties[HealthCause.BLURRED] = int(round(10 + 30 * min(1.0, (0.45 - sharp_rel) / 0.40)))
        color_thr = max(12.0, 3.0 * _ab_spread(ref))
        if not is_grey and (m.color_delta_ab or 0) > color_thr:
            penalties[HealthCause.COLOR_CAST] = int(round(5 + 10 * min(1.0, ((m.color_delta_ab or 0) - color_thr) / 25)))
        noise = _noise_sigma(gray, ref.flat if ref.flat.shape == gray.shape else mask)
        if noise > max(4.0, 2.5 * ref.noise) and HealthCause.DEGRADED not in penalties:
            penalties[HealthCause.NOISE] = int(round(5 + 5 * min(1.0, (noise / ref.noise - 2.5) / 3.0)))

    causes = [c for c in CAUSE_ORDER if c in penalties]
    if any(c in HARD_ZERO for c in causes):
        score = 0
    else:
        score = int(max(0, min(100, 100 - sum(penalties.values()))))
    return Analysis(metrics=m, causes=causes, penalties=penalties, score=score, reference=ref.kind,
                    duration_ms=round((time.perf_counter() - t0) * 1000, 2), is_grey=is_grey)


def _ab_spread(ref: Reference) -> float:
    """Variación «natural» del color de la escena (la referencia ya muy saturada tolera más)."""
    return max(2.0, ref.saturation / 40.0)


def to_check(camera_id: str, analysis: Analysis) -> HealthCheck:
    return HealthCheck(camera_id=camera_id, reference=analysis.reference, score=analysis.score,
                       status=status_for(analysis.score), causes=list(analysis.causes), metrics=analysis.metrics,
                       duration_ms=analysis.duration_ms)


def describe_causes(causes: list[HealthCause], metrics: HealthMetrics) -> list[str]:
    """Frases de tienda con el dato que lo explica («Desenfocada: 35 % de la nitidez de referencia»)."""
    from ..models import HEALTH_CAUSE_ES

    out = []
    for c in causes:
        text = HEALTH_CAUSE_ES.get(str(c), str(c))
        if c == HealthCause.BLURRED and metrics.sharpness_rel is not None:
            text += f": {round(metrics.sharpness_rel * 100)} % de la nitidez de referencia"
        elif c == HealthCause.MOVED and metrics.shift_px is not None:
            text += f": unos {round(metrics.shift_px)} píxeles"
        elif c == HealthCause.ROTATED and metrics.rotation_deg is not None:
            text += f": unos {abs(round(metrics.rotation_deg))} grados"
        out.append(text)
    return out
