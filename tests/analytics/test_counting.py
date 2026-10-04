"""Conteo de línea y zona con detecciones sintéticas (sin modelo ni vídeo)."""
from __future__ import annotations

import pytest

from analytics.counting import LineCounter, ZoneCounter, make_tracker
from tests.analytics.helpers import H, W, path, raw, tracked
from vms.core.models import LineRule, ZoneRule

FPS = 10.0
DT = 1.0 / FPS


def horizontal_line(invert: bool = False) -> LineRule:
    # De izquierda a derecha a media altura. v = (+x, 0); s(p) = v.x * (p.y - y0) > 0 abajo.
    # Por el contrato: pasar de s<0 (arriba) a s>0 (abajo) = ENTRADA.
    return LineRule(id="rule-line0001", camera_id="cam-00000001", name="Puerta",
                    start=(0.1, 0.5), end=(0.9, 0.5), invert=invert)


def counter(rule: LineRule | None = None, **kw: float) -> LineCounter:
    return LineCounter(rule or horizontal_line(), (W, H), fps=FPS, **kw)


def run(lc: LineCounter, frames: list[list[tuple[int, float, float]]], t0: float = 0.0) -> tuple[int, int]:
    tin = tout = 0
    for i, pts in enumerate(frames):
        a, b = lc.update(tracked(pts), t0 + i * DT)
        tin += a
        tout += b
    return tin, tout


def walk(tid: int, x: float, ys: list[float]) -> list[list[tuple[int, float, float]]]:
    return [[(tid, x, y)] for y in ys]


def test_crossing_downwards_is_entry() -> None:
    assert run(counter(), walk(1, 500, path(300, 700, 20))) == (1, 0)


def test_crossing_upwards_is_exit() -> None:
    assert run(counter(), walk(1, 500, path(700, 300, 20))) == (0, 1)


def test_invert_swaps_direction() -> None:
    assert run(counter(horizontal_line(invert=True)), walk(1, 500, path(300, 700, 20))) == (0, 1)


def test_entry_direction_matches_contract_formula() -> None:
    """Línea vertical de arriba abajo: v=(0,+y); s = -v.y*(p.x-x0) → s>0 a la IZQUIERDA. Entrada = ir a la izquierda."""
    rule = LineRule(id="rule-line0002", camera_id="cam-00000001", name="Lateral", start=(0.5, 0.1), end=(0.5, 0.9))
    lc = counter(rule)
    frames = [[(1, x, 500)] for x in path(700, 300, 20)]   # de derecha a izquierda
    assert run(lc, frames) == (1, 0)
    assert lc.signed_distance((300, 500)) > 0


def test_both_directions_several_people() -> None:
    frames = []
    ys_in, ys_out = path(200, 800, 30), path(800, 200, 30)
    for i in range(30):
        frames.append([(1, 300, ys_in[i]), (2, 600, ys_out[i]), (3, 450, ys_in[i])])
    assert run(counter(), frames) == (2, 1)


def test_jitter_on_the_line_is_not_counted() -> None:
    """Persona parada encima de la línea: su pie oscila ±10 px. Sin anti-rebote contaría muchas veces."""
    ys = [300, 400, 480] + [500 + (10 if i % 2 else -10) for i in range(60)] + [480, 400, 300]
    lc = counter()
    assert run(lc, walk(1, 500, ys)) == (0, 0)


def test_jitter_without_hysteresis_would_double_count() -> None:
    """Control: con banda muerta 0 y 1 imagen de confirmación el mismo rebote SÍ cuenta (justifica el diseño)."""
    ys = [300, 450] + [500 + (15 if (i // 2) % 2 else -15) for i in range(40)] + [450, 300]
    no_hyst = counter(hysteresis=0.0, min_seconds=0.0)
    n_in, n_out = run(no_hyst, walk(1, 500, ys))
    assert n_in + n_out > 2
    assert run(counter(), walk(1, 500, ys)) == (0, 0)


def test_stepping_over_and_back_counts_in_and_out() -> None:
    """Entra claramente y vuelve a salir: una entrada y una salida (es lo que pasó)."""
    ys = path(300, 700, 15) + [700] * 5 + path(700, 300, 15)
    assert run(counter(), walk(1, 500, ys)) == (1, 1)


def test_short_detection_gap_while_crossing_still_counts() -> None:
    """El detector pierde a la persona 6 imágenes (0,6 s) justo al cruzar: se recuerda y cuenta."""
    ys = path(300, 700, 20)
    frames = [[(1, 500, y)] if not (8 <= i < 14) else [] for i, y in enumerate(ys)]
    assert run(counter(), frames) == (1, 0)


def test_crossing_outside_segment_is_ignored() -> None:
    """La línea va de x=100 a x=900: alguien que pasa por x=950 no cruza la puerta."""
    assert run(counter(), walk(1, 950, path(300, 700, 20))) == (0, 0)


def test_unconfirmed_tracks_are_ignored() -> None:
    assert run(counter(), walk(-1, 500, path(300, 700, 20))) == (0, 0)


def test_same_track_not_counted_twice_for_one_crossing() -> None:
    ys = path(300, 700, 20) + [700] * 30
    assert run(counter(), walk(7, 500, ys)) == (1, 0)


def test_with_real_bytetrack_from_raw_detections() -> None:
    """Detecciones crudas → ByteTrack (trackers) → línea: dos personas en sentidos opuestos."""
    trk = make_tracker(FPS, confidence=0.5)
    lc = counter()
    down, up = path(250, 750, 40), path(750, 250, 40)
    tin = tout = 0
    for i in range(40):
        det = raw([(300, down[i]), (700, up[i])])
        tr = trk.update(det, timestamp=i * DT)
        a, b = lc.update(tr, i * DT)
        tin += a
        tout += b
    assert (tin, tout) == (1, 1)
    assert (lc.total_in, lc.total_out) == (1, 1)


# =========================================================================== zona
def zone(threshold: int = 3) -> ZoneRule:
    return ZoneRule(id="rule-zone0001", camera_id="cam-00000002", name="Cola cajas 1-3",
                    polygon=[(0.0, 0.5), (1.0, 0.5), (1.0, 1.0), (0.0, 1.0)], alert_threshold=threshold)


def test_zone_occupancy_counts_feet_inside() -> None:
    zc = ZoneCounter(zone(), (W, H))
    det = tracked([(1, 100, 900), (2, 500, 600), (3, 500, 300), (4, 800, 999)])
    assert zc.count(det, 0.5) == 3


def test_zone_counts_confident_new_people_but_not_weak_unconfirmed() -> None:
    zc = ZoneCounter(zone(), (W, H))
    det = tracked([(-1, 100, 900), (-1, 500, 600)])
    det.confidence[1] = 0.3   # detección débil sin seguimiento: solo ayuda a mantener tracks
    assert zc.count(det, 0.5) == 1


def test_zone_empty() -> None:
    assert ZoneCounter(zone(), (W, H)).count(tracked([]), 0.5) == 0


@pytest.mark.parametrize("fps,expected", [(12.0, 2), (2.0, 1), (1.0, 1)])
def test_min_frames_scales_with_fps(fps: float, expected: int) -> None:
    assert LineCounter(horizontal_line(), (W, H), fps=fps).min_frames == expected
