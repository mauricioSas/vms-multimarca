"""Regresiones de la revisión de robustez de la analítica.

- Cola local con índice en memoria (no lista la carpeta en cada segundo).
- Disco lleno: los minutos cerrados no se pierden y el aviso por Telegram sale igualmente.
- Reloj que retrocede: un minuto ya enviado no se sobrescribe con un conteo parcial.
- `clear_below` >= umbral (config.json antiguo): la alerta no se cierra con la cola llena.
"""
from __future__ import annotations

import asyncio
import errno
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest

from analytics import storage as storage_mod
from analytics.aggregation import MinuteAggregator, line_record, minute_of
from analytics.alerts import AlertEvent, QueueAlertMonitor
from analytics.config import AlertsInfo, AnalyticsConfig, SiteInfo
from analytics.service import AnalyticsService
from analytics.settings import AnalyticsSettings
from analytics.storage import Persistence, PgStore, Spool
from vms.core.models import ZoneRule
from vms.core.settings import VmsSettings

M0 = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


def _disk_full(*_a: Any, **_k: Any) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


# --------------------------------------------------------------------------- cola en disco
def test_spool_index_does_not_list_directory_each_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = Spool(tmp_path / "spool")
    for i in range(30):
        spool.append([{"type": "line", "n": i}])
    calls = {"glob": 0}
    real_glob = Path.glob

    def counting_glob(self: Path, pattern: str) -> Any:
        calls["glob"] += 1
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", counting_glob)
    for i in range(30):
        spool.append([{"type": "line", "n": 100 + i}])
        assert spool.pending() == 31 + i
    spool.remove(spool.head(1)[0])
    assert spool.pending() == 59 and calls["glob"] == 0
    # Un proceso nuevo vuelve a leer la carpeta una sola vez
    fresh = Spool(tmp_path / "spool")
    assert fresh.pending() == 59 and fresh.pending() == 59 and calls["glob"] == 1


def test_spool_pending_is_fast_with_a_month_of_batches(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool", max_files=60_000)
    for i in range(3000):
        (spool.dir / f"{i:020d}-1-{i:06d}.jsonl").write_text('{"type":"line"}\n', encoding="utf-8")
    assert spool.pending() == 3000   # carga inicial
    t0 = time.perf_counter()
    for _ in range(1000):
        spool.pending()
    assert time.perf_counter() - t0 < 0.05   # antes: un listado de la carpeta por llamada


async def test_disk_full_keeps_batches_in_memory_and_writes_them_to_postgres(
        pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    p = Persistence(Spool(tmp_path / "spool"), PgStore(pg_dsn, "site-disk-001"))
    monkeypatch.setattr(storage_mod, "atomic_write_text", _disk_full)
    p.submit([line_record("rule-00000001", "cam-00000001", M0, 7, 2)])   # no lanza
    assert p.memory_pending == 1 and p.spool.pending() == 0 and "No space" in p.disk_error
    assert await p.drain() == 1                                           # directo a PostgreSQL
    assert p.memory_pending == 0 and p.ok
    await p.store.close()  # type: ignore[union-attr]
    with psycopg.connect(pg_dsn) as c:
        row = c.execute("SELECT count_in, count_out FROM line_counts_minute WHERE site_id='site-disk-001'").fetchone()
    assert row == (7, 2)
    assert "No se puede escribir la cola local" in caplog.text


async def test_disk_full_and_db_down_then_disk_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = Persistence(Spool(tmp_path / "spool"), None, max_memory_batches=3)
    monkeypatch.setattr(storage_mod, "atomic_write_text", _disk_full)
    for i in range(5):
        p.submit([{"type": "line", "n": i}])
    assert p.memory_pending == 3 and p.dropped_batches == 2   # memoria acotada
    monkeypatch.undo()                                         # vuelve a haber disco
    await p.drain()
    assert p.memory_pending == 0 and p.spool.pending() == 3 and p.disk_error == ""
    assert [p.spool.read(f)[0]["n"] for f in p.spool.files()] == [2, 3, 4]   # en orden


def _alert(kind: str = "started") -> AlertEvent:
    return AlertEvent(kind=kind, alert_id=str(uuid.uuid4()), rule_id="rule-00000002",  # type: ignore[arg-type]
                      camera_id="cam-00000001", rule_name="Cola", started_at=M0, ended_at=None, peak_people=7,
                      threshold=5, occupancy=7)


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat: str, text: str) -> None:
        self.sent.append(text)

    async def aclose(self) -> None:
        return None


async def test_flush_and_alert_survive_full_disk(settings: VmsSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AnalyticsConfig(site=SiteInfo(id="site-test", name="T"),
                          alerts=AlertsInfo(telegram_enabled=True, telegram_chat_id="-1"))
    notifier = _Notifier()
    svc = AnalyticsService(settings, AnalyticsSettings(_env_file=None),  # type: ignore[call-arg]
                           static_config=cfg, notifier=notifier)  # type: ignore[arg-type]
    await svc.start()
    try:
        monkeypatch.setattr(storage_mod, "atomic_write_text", _disk_full)
        old = time.time() - 180
        svc.aggregator.add_line("rule-00000001", "cam-00000001", old, 3, 1)
        await asyncio.sleep(1.5)                       # una vuelta del bucle de envío
        assert svc.aggregator.open_minutes() == 0
        assert svc.persistence is not None and svc.persistence.memory_pending >= 1   # no se perdió
        svc._handle_alert(_alert())
        await asyncio.sleep(0.2)
        assert notifier.sent and "COLA EN CAJAS" in notifier.sent[0]
        st = svc.status()
        assert st["db"]["disk_error"] and st["db"]["memory_pending"] >= 1
    finally:
        monkeypatch.undo()
        await svc.stop()


async def test_retry_wait_is_checked_before_counting_files(settings: VmsSettings) -> None:
    svc = AnalyticsService(settings, AnalyticsSettings(_env_file=None),  # type: ignore[call-arg]
                           static_config=AnalyticsConfig(site=SiteInfo(id="site-test")))
    await svc.start()
    try:
        assert svc.persistence is not None
        svc.persistence.ok = False
        svc._last_drain_fail = time.monotonic()

        def boom() -> bool:
            raise AssertionError("no debe contar la cola mientras espera para reintentar")

        svc.persistence.has_pending = boom  # type: ignore[method-assign]
        await svc._maybe_drain()
    finally:
        svc.persistence.has_pending = lambda: False  # type: ignore[method-assign,union-attr]
        await svc.stop()


# --------------------------------------------------------------------------- reloj que retrocede
def test_clock_step_back_does_not_reopen_a_sent_minute() -> None:
    agg = MinuteAggregator()
    base = 1_790_000_000 - (1_790_000_000 % 60)          # inicio de un minuto
    for i in range(60):
        agg.add_line("rule-door0001", "cam-door0001", base + i * 0.9, 1, 0)
    first = agg.pop_closed(base + 66)
    assert [r["count_in"] for r in first] == [60]
    # w32time/NTP corrige el reloj 10 s hacia atrás: llegan muestras con la hora del minuto ya enviado
    for _ in range(4):
        agg.add_line("rule-door0001", "cam-door0001", base + 55, 1, 0)
    assert agg.late_samples == 4
    second = agg.pop_all()
    assert len(second) == 1 and second[0]["count_in"] == 4
    assert second[0]["minute"] == minute_of(base + 60).isoformat()   # al minuto siguiente, no al ya enviado


# --------------------------------------------------------------------------- clear_below
def test_clear_below_above_threshold_is_clamped_at_runtime() -> None:
    rule = ZoneRule(id="rule-00000002", camera_id="cam-00000001", name="Cola", alert_threshold=5,
                    alert_min_seconds=10, alert_cooldown_seconds=600, clear_below=8,
                    polygon=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])
    assert rule.clear_below_problem()
    mon = QueueAlertMonitor(rule)
    events: list[tuple[float, str, int]] = []
    for t in range(121):                       # 7 personas constantes durante 2 minutos
        for ev in mon.update(7, float(t), M0):
            events.append((t, ev.kind, ev.occupancy))
    assert events == [(10, "started", 7)]      # antes: también («ended» con 7 personas) a los 41 s
