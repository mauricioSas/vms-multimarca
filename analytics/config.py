"""Configuración que la analítica recibe del backend (docs/CONTRATO.md §8.2).

El backend VMS es el dueño de la configuración (cámaras, reglas, alertas). La analítica la pide
cada 30 s a `GET /api/internal/analytics/config` con el token interno y la cabecera
`If-None-Match` (si nada cambió, el backend responde 304 y no se transfiere nada).

Si el backend no responde (se reinicia, se actualiza...), la analítica sigue trabajando con la
última configuración buena, que guarda en `<datos>/analytics/config-cache.json`. Así un corte del
backend no detiene el conteo.

La configuración nunca contiene contraseñas: las URLs RTSP son locales (MediaMTX en 127.0.0.1) y
el token de Telegram se lee del entorno de este proceso.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vms.core.atomic import atomic_write_text
from vms.core.models import AnalyticsRule, AppConfig, DetectorModel, LineRule, ZoneRule
from vms.core.naming import mtx_path

log = logging.getLogger("analytics.config")

CONFIG_PATH = "/api/internal/analytics/config"
INTERNAL_TOKEN_HEADER = "X-VMS-Internal-Token"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SiteInfo(_Model):
    id: str
    name: str = "Sede"
    timezone: str = "Europe/Madrid"
    code: str = ""


class AlertsInfo(_Model):
    telegram_enabled: bool = False
    telegram_chat_id: str | None = None


class CameraJob(_Model):
    """Una cámara a analizar, con sus reglas (líneas de puerta y zonas de cola)."""

    camera_id: str
    name: str = ""
    rtsp_url: str
    fps: float = Field(2.0, ge=0.5, le=30.0)
    detector: DetectorModel = "rfdetr-nano"
    confidence: float = Field(0.5, ge=0.1, le=0.95)
    rules: list[AnalyticsRule] = Field(default_factory=list)

    def active_rules(self) -> list[LineRule | ZoneRule]:
        return [r for r in self.rules if r.enabled]

    def pipeline_key(self) -> str:
        """Lo que obliga a reiniciar el flujo de vídeo/detector si cambia (las reglas no)."""
        return json.dumps([self.rtsp_url, self.fps, self.detector, self.confidence])


class AnalyticsConfig(_Model):
    revision: int = 0
    site: SiteInfo
    alerts: AlertsInfo = Field(default_factory=AlertsInfo)
    cameras: list[CameraJob] = Field(default_factory=list)

    def camera(self, camera_id: str) -> CameraJob | None:
        return next((c for c in self.cameras if c.camera_id == camera_id), None)


def build_analytics_config(cfg: AppConfig, revision: int, site_id: str,
                           rtsp_read_url: Callable[[str, str], str]) -> AnalyticsConfig:
    """Construye el documento §8.2 a partir de config.json.

    La usa la prueba de integración y la puede reutilizar el backend para servir
    `/api/internal/analytics/config` (así hay una única implementación de las reglas de §8.2):
    solo cámaras habilitadas, con analítica activada y con al menos una regla activa.
    `rtsp_read_url(camera_id, stream)` debe devolver la URL local de MediaMTX, sin credenciales.
    """
    s = cfg.settings
    cameras: list[CameraJob] = []
    for a in cfg.analytics_cameras:
        cam = cfg.camera(a.camera_id)
        if not a.enabled or cam is None or not cam.enabled:
            continue
        rules = [r for r in cfg.rules_of(cam.id) if r.enabled]
        if not rules:
            continue
        stream = a.stream if (a.stream == "main" or cam.has_sub) else "main"
        cameras.append(CameraJob(camera_id=cam.id, name=cam.name, rtsp_url=rtsp_read_url(cam.id, stream),
                                 fps=a.fps, detector=a.detector, confidence=a.confidence, rules=rules))
    return AnalyticsConfig(
        revision=revision,
        site=SiteInfo(id=site_id, name=s.site.name, timezone=s.site.timezone, code=s.site.code),
        alerts=AlertsInfo(telegram_enabled=s.alerts.telegram_enabled, telegram_chat_id=s.alerts.telegram_chat_id),
        cameras=cameras)


def default_rtsp_read_url(rtsp_address: str) -> Callable[[str, str], str]:
    """`rtsp_read_url` para `build_analytics_config` a partir de VMS_MTX_RTSP_ADDRESS."""
    host, _, port = rtsp_address.rpartition(":")
    if not host or host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"

    def url(camera_id: str, stream: str) -> str:
        return f"rtsp://{host}:{port}/{mtx_path(camera_id, stream)}"  # type: ignore[arg-type]

    return url


class ConfigSource:
    """Obtiene la configuración del backend con caché local.

    `fetch()` devuelve una configuración nueva o None si no hubo cambios (o si el backend no
    responde y ya tenemos una). Nunca lanza por un fallo de red: lo registra y sigue.
    """

    def __init__(self, base_url: str, token: str, cache_file: Path, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport,
                                         headers={INTERNAL_TOKEN_HEADER: token})
        self._cache_file = cache_file
        self._etag: str | None = None
        self.current: AnalyticsConfig | None = None
        self.last_error = ""
        self._warned_offline = False

    async def aclose(self) -> None:
        await self._client.aclose()

    def load_cache(self) -> AnalyticsConfig | None:
        if not self._cache_file.is_file():
            return None
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            cfg = AnalyticsConfig.model_validate(data["config"])
            self._etag = data.get("etag")
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            log.warning("La caché de configuración no es válida y se ignora: %s", exc)
            return None
        log.info("Configuración cargada de la caché local (revisión %s)", cfg.revision)
        return cfg

    def _save_cache(self, cfg: AnalyticsConfig) -> None:
        try:
            payload: dict[str, Any] = {"etag": self._etag, "config": cfg.model_dump(mode="json")}
            atomic_write_text(self._cache_file, json.dumps(payload, ensure_ascii=False, indent=1))
        except OSError as exc:
            log.warning("No se pudo guardar la caché de configuración: %s", exc)

    async def fetch(self) -> AnalyticsConfig | None:
        headers = {"If-None-Match": self._etag} if (self._etag and self.current) else {}
        try:
            resp = await self._client.get(CONFIG_PATH, headers=headers)
        except httpx.HTTPError as exc:
            return self._offline(f"El backend no responde ({type(exc).__name__}: {exc})")
        if resp.status_code == 304:
            self.last_error = ""
            return None
        if resp.status_code in (401, 403):
            return self._offline("El backend rechaza el token interno (revisa VMS_INTERNAL_TOKEN)")
        if resp.status_code != 200:
            return self._offline(f"El backend respondió {resp.status_code} al pedir la configuración")
        try:
            cfg = AnalyticsConfig.model_validate(resp.json())
        except (ValueError, ValidationError) as exc:
            return self._offline(f"Configuración recibida no válida: {exc}")
        self._etag = resp.headers.get("ETag")
        self.last_error = ""
        if self._warned_offline:
            log.info("El backend vuelve a responder")
            self._warned_offline = False
        if self.current is not None and cfg.model_dump() == self.current.model_dump():
            return None
        self.current = cfg
        self._save_cache(cfg)
        return cfg

    def _offline(self, message: str) -> AnalyticsConfig | None:
        self.last_error = message
        if not self._warned_offline:
            log.warning("%s; se sigue con la última configuración conocida", message)
            self._warned_offline = True
        if self.current is None:
            cached = self.load_cache()
            if cached is not None:
                self.current = cached
                return cached
        return None


def load_config_file(path: Path) -> AnalyticsConfig:
    """Configuración fija desde un archivo JSON (pruebas, piloto sin backend)."""
    return AnalyticsConfig.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
