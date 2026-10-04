"""Telegram con servidor HTTP simulado y obtención de configuración del backend."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from analytics.alerts import AlertEvent
from analytics.config import (
    INTERNAL_TOKEN_HEADER,
    AnalyticsConfig,
    ConfigSource,
    build_analytics_config,
    default_rtsp_read_url,
)
from analytics.telegram import TelegramError, TelegramNotifier, alert_ended_text, alert_started_text
from vms.core.logging_setup import RedactingFormatter
from vms.core.models import AppConfig, Camera, CameraAnalytics, Device, LineRule, SystemSettings, ZoneRule
from vms.core.settings import VmsSettings

TOKEN = "123456789:AAH-secreto_de_prueba"


class Recorder:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def notifier(rec: Recorder) -> TelegramNotifier:
    return TelegramNotifier(TOKEN, transport=httpx.MockTransport(rec), retry_delays=(0.01, 0.01, 0.01))


async def test_send_ok_payload() -> None:
    rec = Recorder([httpx.Response(200, json={"ok": True})])
    n = notifier(rec)
    await n.send("-100123", "Hola")
    await n.aclose()
    (req,) = rec.requests
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "-100123" and body["text"] == "Hola"


async def test_retries_on_429_and_5xx_then_succeeds() -> None:
    rec = Recorder([httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                              "parameters": {"retry_after": 0}}),
                    httpx.Response(502, text="bad gateway"),
                    httpx.Response(200, json={"ok": True})])
    n = notifier(rec)
    await n.send("1", "x")
    await n.aclose()
    assert len(rec.requests) == 3


async def test_network_error_exhausts_retries() -> None:
    rec = Recorder([httpx.ConnectError("sin red")])
    n = notifier(rec)
    with pytest.raises(TelegramError, match="sin conexión"):
        await n.send("1", "x")
    await n.aclose()
    assert len(rec.requests) == 4   # 1 + 3 reintentos


async def test_permanent_errors_are_not_retried() -> None:
    rec = Recorder([httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})])
    n = notifier(rec)
    with pytest.raises(TelegramError, match="no encuentra el chat"):
        await n.send("1", "x")
    rec401 = Recorder([httpx.Response(401, json={"ok": False, "description": "Unauthorized"})])
    n2 = notifier(rec401)
    with pytest.raises(TelegramError, match="token"):
        await n2.send("1", "x")
    await n.aclose()
    await n2.aclose()
    assert len(rec.requests) == 1 and len(rec401.requests) == 1


def test_token_is_redacted_in_logs() -> None:
    rec = logging.LogRecord("analytics.telegram", logging.WARNING, __file__, 1,
                            "fallo en https://api.telegram.org/bot%s/sendMessage", (TOKEN,), None)
    out = RedactingFormatter("%(message)s").format(rec)
    assert "AAH-secreto" not in out and "123456789" not in out


def test_alert_texts_in_spanish_local_time() -> None:
    start = datetime(2026, 10, 5, 11, 40, tzinfo=timezone.utc)   # 13:40 en Madrid (CEST)
    ev = AlertEvent("started", "a1", "rule-z", "cam-1", "Cola cajas 1-3", start, None, 8, 5, 7)
    text = alert_started_text(ev, "Tienda Gràcia", "Cajas", "Europe/Madrid")
    assert "13:40" in text and "7 personas" in text and "Tienda Gràcia" in text
    end = AlertEvent("ended", "a1", "rule-z", "cam-1", "Cola cajas 1-3", start, start + timedelta(minutes=12), 9, 5, 3)
    text2 = alert_ended_text(end, "Tienda Gràcia", "Cajas", "Europe/Madrid")
    assert "12 minutos" in text2 and "máximo 9" in text2
    for t in (text, text2):
        assert "vos " not in t and "tenés" not in t


# =========================================================================== configuración
def sample_app_config() -> AppConfig:
    dev = Device(id="dev-00000001", name="NVR", vendor="hikvision", kind="nvr", host="192.168.1.10")
    cam1 = Camera(id="cam-00000001", name="Puerta", device_id=dev.id, channel=1)
    cam2 = Camera(id="cam-00000002", name="Cajas", device_id=dev.id, channel=2, has_sub=False)
    cam3 = Camera(id="cam-00000003", name="Almacén", device_id=dev.id, channel=3)
    line = LineRule(id="rule-00000001", camera_id=cam1.id, name="Entrada", start=(0.1, 0.5), end=(0.9, 0.5))
    zone = ZoneRule(id="rule-00000002", camera_id=cam2.id, name="Cola", polygon=[(0, 0), (1, 0), (1, 1)])
    off = ZoneRule(id="rule-00000003", camera_id=cam3.id, name="Off", enabled=False, polygon=[(0, 0), (1, 0), (1, 1)])
    s = SystemSettings()
    s.site.name = "Tienda Gràcia"
    s.alerts.telegram_enabled = True
    s.alerts.telegram_chat_id = "-1001"
    return AppConfig(devices=[dev], cameras=[cam1, cam2, cam3],
                     analytics_cameras=[CameraAnalytics(camera_id=cam1.id, enabled=True, fps=12),
                                        CameraAnalytics(camera_id=cam2.id, enabled=True, fps=1),
                                        CameraAnalytics(camera_id=cam3.id, enabled=True)],
                     analytics_rules=[line, zone, off], settings=s)


def test_build_analytics_config_rules_of_contract() -> None:
    cfg = build_analytics_config(sample_app_config(), 7, "site-bcn-001", default_rtsp_read_url("127.0.0.1:8554"))
    assert cfg.revision == 7 and cfg.site.id == "site-bcn-001" and cfg.site.name == "Tienda Gràcia"
    ids = [c.camera_id for c in cfg.cameras]
    assert ids == ["cam-00000001", "cam-00000002"]          # la 3 no tiene reglas activas
    assert cfg.cameras[0].rtsp_url == "rtsp://127.0.0.1:8554/cam-00000001/sub"
    assert cfg.cameras[1].rtsp_url == "rtsp://127.0.0.1:8554/cam-00000002/main"   # sin subflujo
    assert cfg.alerts.telegram_chat_id == "-1001"


def config_json(revision: int) -> dict[str, object]:
    cfg = build_analytics_config(sample_app_config(), revision, "site-bcn-001",
                                 default_rtsp_read_url("127.0.0.1:8554"))
    return cfg.model_dump(mode="json")


async def test_config_source_etag_cache_and_offline(tmp_path: Path) -> None:
    state = {"mode": "ok", "rev": 1}
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if state["mode"] == "down":
            raise httpx.ConnectError("backend parado")
        if req.headers.get(INTERNAL_TOKEN_HEADER) != "tok":
            return httpx.Response(401)
        etag = f'"{state["rev"]}"'
        if req.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, json=config_json(int(state["rev"])), headers={"ETag": etag})

    cache = tmp_path / "config-cache.json"
    src = ConfigSource("http://backend", "tok", cache, transport=httpx.MockTransport(handler))
    cfg = await src.fetch()
    assert isinstance(cfg, AnalyticsConfig) and cfg.revision == 1
    assert cache.is_file()
    assert await src.fetch() is None                      # 304: sin cambios
    assert seen[-1].headers["If-None-Match"] == '"1"'
    state["rev"] = 2
    cfg2 = await src.fetch()
    assert cfg2 is not None and cfg2.revision == 2
    state["mode"] = "down"
    assert await src.fetch() is None and "no responde" in src.last_error
    await src.aclose()

    # Proceso nuevo con el backend caído: arranca desde la caché.
    src2 = ConfigSource("http://backend", "tok", cache, transport=httpx.MockTransport(handler))
    cfg3 = await src2.fetch()
    assert cfg3 is not None and cfg3.revision == 2
    await src2.aclose()


async def test_config_source_bad_token(tmp_path: Path) -> None:
    src = ConfigSource("http://backend", "malo", tmp_path / "c.json",
                       transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert await src.fetch() is None
    assert "token interno" in src.last_error
    await src.aclose()


def test_service_uses_configured_telegram_api_base(settings: VmsSettings) -> None:
    """VMS_ANALYTICS_TELEGRAM_API_BASE (servidor Bot API propio o proxy) llega al notificador."""
    from pydantic import SecretStr

    from analytics.service import AnalyticsService
    from analytics.settings import AnalyticsSettings

    s = settings.model_copy(update={"telegram_bot_token": SecretStr("123:abc")})
    a = AnalyticsSettings(_env_file=None, telegram_api_base="http://127.0.0.1:9/")  # type: ignore[call-arg]
    svc = AnalyticsService(s, a)
    assert svc.notifier is not None
    assert svc.notifier._url == "http://127.0.0.1:9/bot123:abc/sendMessage"  # noqa: SLF001
