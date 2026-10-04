"""Ajustes propios del proceso de analítica (variables de entorno con prefijo VMS_ANALYTICS_).

Los ajustes comunes (carpeta de datos, sede, PostgreSQL, token de Telegram, proveedor LLM...)
vienen de `vms.core.settings.VmsSettings`. Aquí solo están los que únicamente le importan a la
analítica. Todos tienen un valor por defecto razonable: en una instalación normal no hace falta
tocar ninguno.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from vms.core.paths import install_dir
from vms.core.settings import VmsSettings, env_files

InferenceBackend = Literal["auto", "openvino", "onnxruntime", "torch"]


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VMS_ANALYTICS_", extra="ignore",
                                      env_file_encoding="utf-8", env_ignore_empty=True)

    # --- de dónde sale la configuración --------------------------------------------------
    backend_url: str | None = Field(
        None, description="URL del backend VMS; vacío = http://127.0.0.1:<VMS_HTTP_PORT>")
    config_poll_seconds: float = Field(30.0, ge=2.0, le=3600.0)

    # --- modelo de detección -------------------------------------------------------------
    models_dir: Path | None = Field(None, description="Carpeta con los modelos exportados; vacío = <instalación>/models")
    inference_backend: InferenceBackend = "auto"
    # Precisión de OpenVINO. f32 da los mismos resultados que el modelo original; en CPU ARM el
    # valor por defecto de OpenVINO es f16, que en nuestras pruebas perdía la mitad de las personas.
    openvino_precision: Literal["f32", "bf16", "f16"] = "f32"
    inference_threads: int = Field(0, ge=0, le=64, description="Hilos por modelo; 0 = automático")

    # --- conteo --------------------------------------------------------------------------
    line_hysteresis: float = Field(
        0.02, ge=0.0, le=0.2,
        description="Banda muerta a cada lado de la línea (fracción de la diagonal de la imagen)")
    line_min_seconds: float = Field(
        0.15, ge=0.0, le=2.0,
        description="Tiempo mínimo al otro lado de la línea para dar el cruce por bueno")
    track_memory_seconds: float = Field(
        1.5, ge=0.2, le=10.0,
        description="Cuánto se recuerda a una persona que el detector pierde un momento")

    # --- alertas -------------------------------------------------------------------------
    telegram_api_base: str = Field(
        "https://api.telegram.org",
        description="URL de la Bot API de Telegram (cámbiala solo si usas un servidor Bot API propio o un proxy)")
    notify_clear: bool = Field(True, description="Enviar también el aviso de «cola normalizada»")
    alert_clear_seconds: float = Field(30.0, ge=0.0, le=3600.0)

    # --- persistencia y estado -----------------------------------------------------------
    minute_close_margin_seconds: float = Field(5.0, ge=0.0, le=60.0)
    status_interval_seconds: float = Field(10.0, ge=1.0, le=300.0)
    db_retry_seconds: float = Field(15.0, ge=1.0, le=600.0)


def load_analytics_settings(env_file: Path | None = None, **overrides: object) -> AnalyticsSettings:
    files = [env_file] if env_file else env_files()
    return AnalyticsSettings(_env_file=[str(f) for f in files] or None, **overrides)  # type: ignore[call-arg]


def resolve_backend_url(settings: VmsSettings, analytics: AnalyticsSettings) -> str:
    if analytics.backend_url:
        return analytics.backend_url.rstrip("/")
    host = settings.http_host
    if host in ("0.0.0.0", "::", "", "[::]"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.http_port}"


def resolve_models_dir(settings: VmsSettings, analytics: AnalyticsSettings) -> Path:
    """Carpeta de modelos: ajuste explícito → <instalación>/models → <datos>/models."""
    if analytics.models_dir:
        return Path(analytics.models_dir)
    candidate = install_dir() / "models"
    if candidate.is_dir():
        return candidate
    return settings.paths.base / "models"
