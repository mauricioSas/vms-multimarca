"""Salud de imagen con imágenes SINTÉTICAS (CONTRATO §18.19, PLAN-V2 §6.2 B6 criterio 1).

Para cada causa: la causa esperada y su puntuación. 50 fotogramas normales sin falsos positivos. Menos de
20 ms por comprobación a 640 px. Histéresis. Puerta de inliers (lección del prototipo: con la imagen
desenfocada ORB da desplazamientos falsos). Nunca se usan fotos ni personas: ver `synthetic.py`.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest

from tests.ops import synthetic as syn
from vms.ops.health import imaging as im
from vms.ops.health.tracker import HealthTracker
from vms.ops.models import HealthCause as C
from vms.ops.models import HealthCheck


@pytest.fixture(scope="module")
def base() -> np.ndarray:
    return syn.scene(1)


@pytest.fixture(scope="module")
def day(base: np.ndarray) -> im.Reference:
    built = im.build_reference(syn.reference_frames(base))
    assert not built.person_warning, "tomar la referencia con la tienda «vacía» no debe avisar"
    return im.Reference.from_image(built.image, "day")


@pytest.fixture(scope="module")
def night(base: np.ndarray) -> im.Reference:
    return im.Reference.from_image(im.build_reference([syn.to_grey(f, 0.8) for f in syn.reference_frames(base)]).image,
                                   "night")


# (alteración, causa esperada, puntuación mínima, puntuación máxima, estado)
CASES = [
    ("black", C.BLACK, 0, 0, "critical"),
    ("covered", C.COVERED, 0, 0, "critical"),
    ("blurred", C.BLURRED, 50, 70, "warning"),
    ("moved", C.MOVED, 40, 60, "warning"),       # −40 a −60 según los píxeles: aviso o grave
    ("rotated", C.ROTATED, 40, 60, "warning"),
    ("scene_changed", C.SCENE_CHANGED, 40, 40, "critical"),
    ("degraded", C.DEGRADED, 70, 85, "warning"),
    ("backlight", C.BACKLIGHT, 85, 90, "ok"),
    ("color_cast", C.COLOR_CAST, 85, 95, "ok"),
    ("noise", C.NOISE, 90, 95, "ok"),
]


@pytest.mark.parametrize(("name", "cause", "lo", "hi", "status"), CASES, ids=[c[0] for c in CASES])
def test_each_cause_with_expected_score(base: np.ndarray, day: im.Reference, name: str, cause: C, lo: int, hi: int,
                                        status: str) -> None:
    a = im.analyze(syn.alterations(base)[name], day=day, night=None)
    assert a.causes and a.causes[0] == cause, (name, a.causes, a.metrics)
    assert lo <= (a.score or 0) <= hi, (name, a.score, a.penalties)
    assert im.status_for(a.score) == status


def test_fifty_normal_frames_without_false_positives(base: np.ndarray, day: im.Reference) -> None:
    for i in range(50):
        a = im.analyze(syn.normal_frame(base, i), day=day, night=None)
        assert a.causes == [] and a.score == 100, (i, a.causes, a.metrics)


def test_under_20_ms_per_check_at_640_px(base: np.ndarray, day: im.Reference) -> None:
    frames = [cv2.resize(syn.normal_frame(base, i), (640, 360), interpolation=cv2.INTER_AREA) for i in range(30)]
    im.analyze(frames[0], day=day, night=None)   # calentamiento (OpenCV carga sus tablas)
    durations = [im.analyze(f, day=day, night=None).duration_ms for f in frames]
    assert statistics.median(durations) < 20, durations
    assert sorted(durations)[int(len(durations) * 0.9)] < 30, durations   # holgura para CI cargado


def test_frozen_ignores_the_osd_clock(base: np.ndarray, day: im.Reference) -> None:
    frame = syn.normal_frame(base, 5)
    same_image_new_clock = syn.osd(frame, datetime(2026, 10, 5, 23, 59, 59))
    a = im.analyze(same_image_new_clock, day=day, night=None, previous=frame)
    assert a.causes == [C.FROZEN] and a.score == 0
    live = im.analyze(syn.normal_frame(base, 6), day=day, night=None, previous=syn.normal_frame(base, 7))
    assert C.FROZEN not in live.causes and live.score == 100


def test_ir_night_reference_weak_and_stuck(base: np.ndarray, day: im.Reference, night: im.Reference) -> None:
    for i in range(5):   # de noche, en gris, contra la referencia de noche: correcto
        a = im.analyze(syn.to_grey(syn.normal_frame(base, i), 0.8), day=day, night=night)
        assert a.reference == "night" and a.score == 100, a.causes
    weak = im.analyze(syn.to_grey(syn.normal_frame(base, 3), 0.3), day=day, night=night)
    assert weak.causes == [C.IR_WEAK] and weak.score == 75
    stuck_grey = im.analyze(syn.alterations(base)["ir_stuck"], day=day, night=night)
    assert stuck_grey.causes == [C.IR_STUCK] and stuck_grey.score == 70
    dark_colour = im.analyze(syn.jpeg((syn.normal_frame(base, 4) * 0.18).astype(np.uint8)), day=day, night=night)
    assert dark_colour.causes == [C.IR_STUCK], "color y oscura de noche: el filtro IR no cambia"


def test_inlier_gate_never_reports_a_shift_from_bad_matches(base: np.ndarray, day: im.Reference) -> None:
    """Lección 1 del prototipo: con imagen muy borrosa o de otra escena, ORB puede «ver» un desplazamiento
    grande con pocos inliers. Nunca se informa de «movida» por debajo del 15 %."""
    for img in (cv2.GaussianBlur(syn.normal_frame(base, 9), (0, 0), 9), syn.alterations(base)["scene_changed"]):
        a = im.analyze(img, day=day, night=None)
        assert C.MOVED not in a.causes and C.ROTATED not in a.causes, (a.causes, a.metrics)
        if (a.metrics.inliers_ratio or 0) < im.MIN_INLIERS:
            assert a.metrics.shift_px is None or C.MOVED not in a.causes


def test_masks_exclude_dynamic_zones(base: np.ndarray) -> None:
    """Una pantalla de publicidad que se apaga (gris liso): sin máscara cuenta como pérdida de nitidez; con la
    zona excluida, la cámara está bien."""
    mask = [[(0.0, 0.0), (0.6, 0.0), (0.6, 1.0), (0.0, 1.0)]]
    ref = im.Reference.from_image(im.build_reference(syn.reference_frames(base)).image, "day", mask)
    frame = syn.normal_frame(base, 11).copy()
    h, w = frame.shape[:2]
    frame[:, : int(w * 0.6)] = 128
    assert im.analyze(frame, day=ref, night=None, masks=mask).score == 100
    unmasked = im.Reference.from_image(ref.image, "day")
    assert im.analyze(frame, day=unmasked, night=None).score < 100


def test_reference_median_removes_passing_objects_and_warns_if_someone_stays(base: np.ndarray) -> None:
    frames = syn.reference_frames(base)
    built = im.build_reference(frames)
    clean = cv2.resize(base, (640, 360), interpolation=cv2.INTER_AREA)
    diff = float(cv2.absdiff(built.image, clean).mean())
    assert diff < 6, diff                                   # la mediana es la escena, sin los objetos que pasan
    assert all(float(cv2.absdiff(built.image, im.to_work(f)).mean()) > 0.5 for f in frames)  # no es un fotograma suelto
    still = []
    for k, f in enumerate(syn.reference_frames(base)):
        f = f.copy()
        if k % 3:   # un objeto quieto (abstracto) en 2 de cada 3 fotogramas: queda en la mediana
            cv2.rectangle(f, (300, 300), (500, 650), (60, 240, 240), -1)
        still.append(f)
    assert im.build_reference(still).person_warning


def test_no_reference_is_unknown(base: np.ndarray) -> None:
    a = im.analyze(syn.normal_frame(base, 1), day=None, night=None)
    assert a.score is None and a.causes == [C.NO_REFERENCE] and im.status_for(a.score) == "unknown"


def _check(status: str, score: int | None, i: int) -> HealthCheck:
    return HealthCheck(camera_id="cam-00000001", status=status, score=score,
                       at=datetime(2026, 10, 5, tzinfo=timezone.utc) + timedelta(minutes=3 * i))


def test_hysteresis_needs_n_consecutive_checks() -> None:
    tr = HealthTracker(hysteresis=3)
    assert tr.update(_check("ok", 100, 0)) is not None          # desconocido → correcto: en el acto
    assert tr.update(_check("critical", 0, 1)) is None
    assert tr.update(_check("critical", 0, 2)) is None
    assert tr.update(_check("ok", 100, 3)) is None               # un fotograma bueno reinicia la cuenta
    assert tr.update(_check("critical", 0, 4)) is None
    assert tr.update(_check("critical", 0, 5)) is None
    t = tr.update(_check("critical", 0, 6))
    assert t is not None and t.previous == "ok" and t.current == "critical"
    assert tr.get("cam-00000001").since == _check("critical", 0, 6).at
    # y para volver a «correcto» también hacen falta 3 seguidas (no parpadea)
    assert tr.update(_check("ok", 100, 7)) is None
    assert tr.update(_check("ok", 100, 8)) is None
    t = tr.update(_check("ok", 100, 9))
    assert t is not None and t.current == "ok"
    assert tr.get("cam-00000001").status == "ok"


def test_hysteresis_one_is_immediate() -> None:
    tr = HealthTracker(hysteresis=1)
    tr.update(_check("ok", 100, 0))
    t = tr.update(_check("warning", 60, 1))
    assert t is not None and t.current == "warning"


def test_describe_causes_in_shop_language(base: np.ndarray, day: im.Reference) -> None:
    a = im.analyze(syn.alterations(base)["blurred"], day=day, night=None)
    text = im.describe_causes(a.causes, a.metrics)[0]
    assert text.startswith("Imagen desenfocada:") and "% de la nitidez de referencia" in text
