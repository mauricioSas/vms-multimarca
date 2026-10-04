"""Integración real: vídeo de personas → NVR simulado → MediaMTX de la sede → analítica → PostgreSQL.

Cadena idéntica a la de producción, sin hardware:
  people-walking.mp4 → ffmpeg → NVR Hikvision simulado (Digest, /Streaming/Channels/102)
  → MediaMTX de la sede (ruta cam-…/sub bajo demanda) → analytics (OpenCV + RF-DETR nano real
  + ByteTrack + LineZone/PolygonZone) → minutos en PostgreSQL (pgserver) + aviso de cola (Telegram simulado).

Imprime las fps y los ms de inferencia medidos en este equipo.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import psycopg
import pytest

from analytics.config import AlertsInfo, AnalyticsConfig, CameraJob, SiteInfo
from analytics.service import AnalyticsService
from analytics.settings import AnalyticsSettings
from analytics.snapshot import grab_jpeg
from analytics.storage import PgStore
from analytics.telegram import TelegramNotifier
from tests.e2e.test_infra_chain import MiniMtx
from tools.camsim.simulator import CameraSimulator, SimDevice
from vms.core.models import LineRule, ZoneRule
from vms.core.settings import VmsSettings

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg,
              pytest.mark.needs_postgres, pytest.mark.needs_model, pytest.mark.timeout(240)]

ROOT = Path(__file__).resolve().parents[2]
CAM = "cam-00000001"
RUN_SECONDS = 35.0
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".mp4", ".mkv", ".avi", ".h264", ".ts"}


def config(url: str) -> AnalyticsConfig:
    line = LineRule(id="rule-00000001", camera_id=CAM, name="Entrada", start=(0.02, 0.5), end=(0.98, 0.5))
    zone = ZoneRule(id="rule-00000002", camera_id=CAM, name="Zona inferior",
                    polygon=[(0.0, 0.55), (1.0, 0.55), (1.0, 1.0), (0.0, 1.0)],
                    alert_threshold=3, alert_min_seconds=2, alert_cooldown_seconds=600)
    return AnalyticsConfig(revision=1, site=SiteInfo(id="site-test", name="Tienda simulada"),
                           alerts=AlertsInfo(telegram_enabled=True, telegram_chat_id="-1001"),
                           cameras=[CameraJob(camera_id=CAM, name="Puerta", rtsp_url=url, fps=10.0,
                                              detector="rfdetr-nano", confidence=0.4, rules=[line, zone])])


async def test_people_video_counts_end_to_end(camsim_factory: Callable[..., CameraSimulator], mediamtx_bin: str,
                                              people_video: Path, pg_dsn: str, settings: VmsSettings,
                                              tmp_path: Path) -> None:
    if not (ROOT / "models" / "rfdetr-nano.onnx").is_file():
        pytest.skip("Falta models/rfdetr-nano.onnx: python -m analytics.tools.export_model")
    sim = camsim_factory([SimDevice("hik1", "hikvision", channels=1, videos={1: people_video},
                                    main_size=(1280, 720), sub_size=(640, 360), fps=15)])
    site_mtx = MiniMtx(mediamtx_bin, tmp_path / "site-mtx")
    try:
        site_mtx.add_path(f"{CAM}/sub", {"source": sim.device("hik1").rtsp_url(1, "sub"), "sourceOnDemand": True})
        url = f"rtsp://127.0.0.1:{site_mtx.rtsp_port}/{CAM}/sub"

        jpeg = await asyncio.to_thread(grab_jpeg, url)          # captura para el editor de zonas, en memoria
        assert jpeg[:2] == b"\xff\xd8" and len(jpeg) > 5000

        sent: list[str] = []

        def telegram(req: httpx.Request) -> httpx.Response:
            sent.append(json.loads(req.content)["text"])
            return httpx.Response(200, json={"ok": True})

        notifier = TelegramNotifier("1:abc", transport=httpx.MockTransport(telegram), retry_delays=(0.01,))
        asettings = AnalyticsSettings(_env_file=None, models_dir=ROOT / "models",  # type: ignore[call-arg]
                                      status_interval_seconds=2)
        svc = AnalyticsService(settings, asettings, static_config=config(url), notifier=notifier,
                               store=PgStore(pg_dsn, "site-test"))
        await svc.start()
        samples = []
        try:
            for _ in range(int(RUN_SECONDS / 5)):
                await asyncio.sleep(5)
                samples.append(svc.pipelines[CAM].status())
        finally:
            await svc.stop(flush_open_minutes=True)
            await notifier.aclose()
        assert sim.proxy_connections("hik1") >= 1   # se leyó a través de MediaMTX, no directo al NVR
    finally:
        site_mtx.stop()

    last = samples[-1]
    print(f"\n[analítica] estado={last['state']} backend={last['detector']['backend']} "
          f"fps_in={last['fps_in']} fps_procesadas={last['fps_processed']} "
          f"inferencia p50={last['inference_ms_p50']} ms p95={last['inference_ms_p95']} ms "
          f"imágenes={last['frames_processed']} totales={last['totals']} ocupación={last['occupancy']}")
    assert last["state"] == "running" and last["frame_size"] == [640, 360]
    assert last["frames_processed"] > 100

    with psycopg.connect(pg_dsn) as conn:
        cin, cout = conn.execute("SELECT COALESCE(SUM(count_in),0), COALESCE(SUM(count_out),0) "
                                 "FROM line_counts_minute WHERE rule_id = 'rule-00000001'").fetchone()  # type: ignore[misc]
        zmax, zsamples = conn.execute("SELECT MAX(max_people), SUM(samples) FROM zone_occupancy_minute").fetchone()  # type: ignore[misc]
        n_alerts = conn.execute("SELECT COUNT(*) FROM queue_alerts WHERE notified_at IS NOT NULL").fetchone()[0]  # type: ignore[index]
    print(f"[analítica] PostgreSQL: entradas={cin} salidas={cout} ocupación máx={zmax} muestras={zsamples} "
          f"alertas avisadas={n_alerts} mensajes Telegram={len(sent)}")
    assert cin + cout > 0, "no se contó ningún cruce"
    assert cin > 0 and cout > 0, "el vídeo tiene gente en ambos sentidos"
    assert zmax is not None and zmax > 0 and zsamples > 100
    assert n_alerts >= 1 and any("COLA EN CAJAS" in t for t in sent)

    # RGPD: ni la carpeta de datos ni la de trabajo contienen imágenes o vídeo de la analítica.
    leaked = [p for p in settings.paths.base.rglob("*") if p.suffix.lower() in IMAGE_EXT]
    assert leaked == [], leaked
