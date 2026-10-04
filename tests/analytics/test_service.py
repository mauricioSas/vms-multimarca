"""Servicio completo con vídeo y detector simulados: conteo → PostgreSQL, alerta → Telegram, status.json.

No usa el modelo real (eso lo cubre test_integration_rtsp.py): aquí se controla exactamente qué
«ve» el detector para comprobar que cada pieza del servicio hace lo que debe.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import httpx
import numpy as np
import psycopg
import pytest
import supervision as sv

from analytics.config import AlertsInfo, AnalyticsConfig, CameraJob, SiteInfo
from analytics.service import AnalyticsService, SingleInstanceLock
from analytics.settings import AnalyticsSettings
from analytics.storage import PgStore
from analytics.telegram import TelegramNotifier
from analytics.video import FrameGrabber
from tests.analytics.test_detector_video import FakeCapture
from vms.core.models import LineRule, ZoneRule
from vms.core.settings import VmsSettings

pytestmark = [pytest.mark.needs_postgres]

SIZE = 200


class ScriptedDetector:
    """Cada llamada avanza un paso: una persona baja cruzando la línea; 6 personas quietas en la zona."""

    model = "rfdetr-nano"
    backend = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def detect(self, image_bgr: np.ndarray, threshold: float) -> sv.Detections:
        assert image_bgr.shape[:2] == (SIZE, SIZE)
        with self.lock:
            i = self.calls
            self.calls += 1
        boxes = []
        if i < 40:                                  # persona andando de y=40 a y=160 en x=50
            y = 40 + 3 * i
            boxes.append([45, y - 20, 55, y])
        for k in range(6):                          # cola: 6 personas quietas en x≥130
            x, y = 135 + (k % 3) * 20, 120 + (k // 3) * 40
            boxes.append([x - 5, y - 20, x + 5, y])
        n = len(boxes)
        return sv.Detections(xyxy=np.array(boxes, dtype=np.float32), confidence=np.full(n, 0.9, dtype=np.float32),
                             class_id=np.zeros(n, dtype=int))


class FakeRegistry:
    def __init__(self) -> None:
        self.detector = ScriptedDetector()

    def get(self, model: str) -> ScriptedDetector:
        return self.detector


class BigFrameCapture(FakeCapture):
    def read(self) -> tuple[bool, np.ndarray | None]:
        ok, _ = super().read()
        return (ok, np.zeros((SIZE, SIZE, 3), dtype=np.uint8) if ok else None)


def static_config() -> AnalyticsConfig:
    line = LineRule(id="rule-00000001", camera_id="cam-00000001", name="Entrada",
                    start=(0.05, 0.5), end=(0.55, 0.5))
    zone = ZoneRule(id="rule-00000002", camera_id="cam-00000001", name="Cola cajas",
                    polygon=[(0.6, 0.0), (1.0, 0.0), (1.0, 1.0), (0.6, 1.0)],
                    alert_threshold=5, alert_min_seconds=1, alert_cooldown_seconds=600)
    return AnalyticsConfig(revision=3, site=SiteInfo(id="site-test", name="Tienda de pruebas"),
                           alerts=AlertsInfo(telegram_enabled=True, telegram_chat_id="-100999"),
                           cameras=[CameraJob(camera_id="cam-00000001", name="Puerta y cajas",
                                              rtsp_url="rtsp://127.0.0.1:1/cam-00000001/sub", fps=20,
                                              confidence=0.5, rules=[line, zone])])


async def test_service_end_to_end_with_fakes(settings: VmsSettings, pg_dsn: str) -> None:
    sent: list[dict[str, object]] = []

    def telegram(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier("1:abc", transport=httpx.MockTransport(telegram), retry_delays=(0.01,))
    asettings = AnalyticsSettings(_env_file=None, status_interval_seconds=1)  # type: ignore[call-arg]
    svc = AnalyticsService(settings, asettings, static_config=static_config(), registry=FakeRegistry(),  # type: ignore[arg-type]
                           notifier=notifier, store=PgStore(pg_dsn, "site-test"),
                           grabber_factory=lambda url, name: FrameGrabber(
                               url, name, capture_factory=lambda u: BigFrameCapture(100_000, delay=0.01)))
    await svc.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.1)
            if svc.alerts_sent >= 1 and svc.registry.detector.calls > 45:  # type: ignore[attr-defined]
                break
        status = json.loads((settings.paths.analytics_dir / "status.json").read_text(encoding="utf-8"))
    finally:
        await svc.stop(flush_open_minutes=True)
        await notifier.aclose()

    cam = status["cameras"][0]
    assert status["running"] is True and status["config_revision"] == 3
    assert cam["state"] == "running" and cam["fps_processed"] > 5 and cam["inference_ms_p50"] is not None
    assert cam["occupancy"]["rule-00000002"] == 6

    with psycopg.connect(pg_dsn) as conn:
        cin, cout = conn.execute("SELECT SUM(count_in), SUM(count_out) FROM line_counts_minute").fetchone()  # type: ignore[misc]
        zmax, zsamples = conn.execute("SELECT MAX(max_people), SUM(samples) FROM zone_occupancy_minute").fetchone()  # type: ignore[misc]
        alerts = conn.execute("SELECT threshold, peak_people, notified_at IS NOT NULL, ended_at IS NOT NULL "
                              "FROM queue_alerts").fetchall()
        rules = conn.execute("SELECT rule_id, kind FROM analytics_rules ORDER BY 1").fetchall()
        site = conn.execute("SELECT name FROM sites").fetchone()
    assert (cin, cout) == (1, 0)
    assert zmax == 6 and zsamples > 10
    assert alerts == [(5, 6, True, True)]           # avisada y cerrada al parar
    assert rules == [("rule-00000001", "line"), ("rule-00000002", "zone")]
    assert site == ("Tienda de pruebas",)

    texts = [str(m["text"]) for m in sent]
    assert len(texts) == 2 and "COLA EN CAJAS" in texts[0] and "Cola normalizada" in texts[1]
    assert all(m["chat_id"] == "-100999" for m in sent)
    final = json.loads((settings.paths.analytics_dir / "status.json").read_text(encoding="utf-8"))
    assert final["running"] is False and final["db"]["spool_pending"] == 0


async def test_open_minute_saved_on_normal_stop_and_restored(settings: VmsSettings, tmp_path: Path) -> None:
    asettings = AnalyticsSettings(_env_file=None)  # type: ignore[call-arg]
    svc = AnalyticsService(settings, asettings, static_config=static_config(), registry=FakeRegistry(),  # type: ignore[arg-type]
                           grabber_factory=lambda url, name: FrameGrabber(
                               url, name, capture_factory=lambda u: BigFrameCapture(100_000, delay=0.01)))
    await svc.start()
    for _ in range(50):
        await asyncio.sleep(0.1)
        if svc.registry.detector.calls > 45:  # type: ignore[attr-defined]
            break
    await svc.stop()   # sin PostgreSQL configurado y sin vaciar el minuto
    open_file = settings.paths.analytics_dir / "open-minutes.json"
    saved = json.loads(open_file.read_text(encoding="utf-8"))
    assert any(r["type"] == "line_open" and r["count_in"] == 1 for r in saved)
    svc2 = AnalyticsService(settings, asettings, static_config=AnalyticsConfig(site=SiteInfo(id="site-test")),
                            registry=FakeRegistry())  # type: ignore[arg-type]
    await svc2.start()
    assert not open_file.exists() and svc2.aggregator.open_minutes() >= 1
    await svc2.stop(flush_open_minutes=True)
    spooled = [json.loads(line) for f in (settings.paths.analytics_dir / "spool").glob("*.jsonl")
               for line in f.read_text(encoding="utf-8").splitlines()]
    assert sum(r.get("count_in", 0) for r in spooled if r["type"] == "line") == 1


def test_single_instance_lock(tmp_path: Path) -> None:
    a, b = SingleInstanceLock(tmp_path / "x.lock"), SingleInstanceLock(tmp_path / "x.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_no_frames_written_to_disk_by_analytics_code() -> None:
    """RGPD (revisión estática): el código de la analítica no tiene ninguna llamada que escriba imágenes."""
    import re

    root = Path(__file__).resolve().parents[2] / "analytics"
    pattern = re.compile(r"cv2\.imwrite|VideoWriter|\.save\(|imageio|PIL\.Image|tofile\(")
    hits = [str(f) for f in root.rglob("*.py") if pattern.search(f.read_text(encoding="utf-8"))]
    assert hits == [], hits
