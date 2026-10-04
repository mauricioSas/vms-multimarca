"""Lógica «X personas durante Y segundos», cooldown, fin de alerta y agregación por minuto."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analytics.aggregation import MinuteAggregator, minute_of
from analytics.alerts import QueueAlertMonitor
from vms.core.models import ZoneRule

T0 = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)


def rule(**kw: object) -> ZoneRule:
    base = dict(id="rule-zone0001", camera_id="cam-00000002", name="Cola", polygon=[(0, 0), (1, 0), (1, 1)],
                alert_threshold=5, alert_min_seconds=60, alert_cooldown_seconds=600)
    base.update(kw)
    return ZoneRule(**base)  # type: ignore[arg-type]


def feed(mon: QueueAlertMonitor, seq: list[tuple[float, int]]) -> list[tuple[float, str]]:
    """seq = [(segundo, ocupación)] → [(segundo, tipo de evento)]."""
    out = []
    for t, occ in seq:
        for ev in mon.update(occ, t, T0 + timedelta(seconds=t)):
            out.append((t, ev.kind))
    return out


def every_second(start: float, end: float, occ: int) -> list[tuple[float, int]]:
    return [(float(t), occ) for t in range(int(start), int(end))]


def test_alert_after_y_seconds_over_threshold() -> None:
    mon = QueueAlertMonitor(rule())
    ev = feed(mon, every_second(0, 59, 6))
    assert ev == []
    ev = feed(mon, [(60.0, 6)])
    assert ev == [(60.0, "started")]
    assert mon.active is not None and mon.active.started_at == T0  # empieza cuando la cola superó el umbral


def test_short_spike_does_not_alert() -> None:
    mon = QueueAlertMonitor(rule())
    seq = every_second(0, 50, 7) + every_second(50, 55, 2) + every_second(55, 100, 7)
    assert feed(mon, seq) == []   # nunca 60 s SEGUIDOS por encima


def test_exactly_threshold_counts_as_over() -> None:
    mon = QueueAlertMonitor(rule(alert_min_seconds=10))
    assert feed(mon, every_second(0, 11, 5)) == [(10.0, "started")]


def test_alert_ends_after_30s_below_clear_level() -> None:
    mon = QueueAlertMonitor(rule(alert_min_seconds=10))
    feed(mon, every_second(0, 11, 8))
    # 4 = umbral-1 = clear_below por defecto. 29 s no bastan; a los 30 s termina.
    ev = feed(mon, every_second(11, 41, 4))
    assert ev == []
    ev = feed(mon, [(41.0, 4)])
    assert ev == [(41.0, "ended")]
    assert mon.active is None


def test_oscillation_around_threshold_keeps_single_alert() -> None:
    mon = QueueAlertMonitor(rule(alert_min_seconds=10))
    seq = every_second(0, 11, 6) + [(float(t), 4 if t % 20 < 10 else 6) for t in range(11, 200)]
    ev = feed(mon, seq)
    assert ev == [(10.0, "started")]   # baja a 4 solo 10 s seguidos: no da por terminada la alerta


def test_cooldown_blocks_second_alert_then_realerts() -> None:
    mon = QueueAlertMonitor(rule(alert_min_seconds=10, alert_cooldown_seconds=300))
    ev = feed(mon, every_second(0, 11, 9))                     # alerta en t=10
    ev += feed(mon, every_second(11, 42, 1))                    # termina en t=41
    ev += feed(mon, every_second(100, 200, 9))                  # vuelve la cola, pero en cooldown
    assert [k for _, k in ev] == ["started", "ended"]
    ev2 = feed(mon, every_second(200, 320, 9))                  # sigue la cola: a los 300 s del aviso anterior
    assert ev2 == [(310.0, "started")]


def test_peak_and_close_on_shutdown() -> None:
    mon = QueueAlertMonitor(rule(alert_min_seconds=5))
    feed(mon, every_second(0, 5, 6) + [(5.0, 11), (6.0, 7)])
    end = mon.close(T0 + timedelta(seconds=7), occupancy=7)
    assert end is not None and end.kind == "ended" and end.peak_people == 11
    assert end.duration_seconds == 7.0
    assert mon.close(T0) is None


def test_threshold_change_keeps_state() -> None:
    mon = QueueAlertMonitor(rule(alert_min_seconds=5))
    feed(mon, every_second(0, 6, 6))
    assert mon.active is not None
    mon.rule = rule(alert_min_seconds=5, alert_threshold=10)
    assert mon.clear_below == 9
    ev = feed(mon, every_second(6, 37, 8))      # 8 <= 9 durante 30 s → termina
    assert [k for _, k in ev] == ["ended"]


def test_custom_clear_below() -> None:
    mon = QueueAlertMonitor(rule(alert_min_seconds=0, clear_below=2), clear_seconds=5)
    assert feed(mon, [(0.0, 5)]) == [(0.0, "started")]
    assert feed(mon, every_second(1, 20, 3)) == []          # 3 > 2: sigue activa
    assert feed(mon, every_second(20, 26, 2)) == [(25.0, "ended")]


# =========================================================================== agregación
def ts(minute: int, second: float = 0.0) -> float:
    return (T0 + timedelta(minutes=minute, seconds=second)).timestamp()


def test_minute_bucket_is_utc_truncated() -> None:
    assert minute_of(ts(3, 59.9)) == T0 + timedelta(minutes=3)


def test_line_minutes_close_with_margin_and_zero_rows() -> None:
    agg = MinuteAggregator()
    agg.add_line("rule-a", "cam-1", ts(0, 10), 2, 1)
    agg.add_line("rule-a", "cam-1", ts(0, 50), 1, 0)
    agg.add_line("rule-a", "cam-1", ts(1, 5), 0, 0)      # minuto sin cruces: fila con ceros
    assert agg.pop_closed(ts(1, 4)) == []                 # minuto 0 cerrado pero dentro del margen
    out = agg.pop_closed(ts(1, 5))
    assert out == [{"type": "line", "rule_id": "rule-a", "camera_id": "cam-1",
                    "minute": (T0).isoformat(), "count_in": 3, "count_out": 1}]
    out = agg.pop_closed(ts(2, 6))
    assert out[0]["count_in"] == 0 and out[0]["count_out"] == 0


def test_zone_avg_max_and_seconds_over_clamped() -> None:
    agg = MinuteAggregator()
    for i, occ in enumerate([2, 4, 6, 8]):
        agg.add_zone("rule-z", "cam-2", ts(0, i * 10), occ, occ >= 5, 10.0)
    agg.add_zone("rule-z", "cam-2", ts(0, 45), 9, True, 100.0)   # dt enorme: se recorta a 60 s
    (rec,) = agg.pop_all()
    assert rec["samples"] == 5
    assert rec["avg_people"] == round((2 + 4 + 6 + 8 + 9) / 5, 3)
    assert rec["max_people"] == 9
    assert rec["seconds_over_threshold"] == 60


def test_open_minutes_survive_restart() -> None:
    a = MinuteAggregator()
    a.add_line("rule-a", "cam-1", ts(0, 10), 3, 2)
    a.add_zone("rule-z", "cam-2", ts(0, 10), 4, False, 1.0)
    saved = a.export_open()
    b = MinuteAggregator()
    assert b.import_open(saved) == 2
    b.add_line("rule-a", "cam-1", ts(0, 40), 1, 0)
    recs = {r["type"]: r for r in b.pop_all()}
    assert recs["line"]["count_in"] == 4 and recs["line"]["count_out"] == 2
    assert recs["zone"]["samples"] == 1
