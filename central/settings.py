"""Ajustes del panel central y del agente de latido de sede.

Se leen de variables de entorno y de los mismos archivos .env que el resto del producto
(vms.core.settings.env_files). Prefijos:

  VMS_CENTRAL_*   servidor central (python -m central)
  VMS_AGENT_*     agente de latido de la sede (python -m central.agent)

Algunos ajustes aceptan también el nombre común del producto (VMS_PG_DSN,
VMS_HEARTBEAT_SECONDS, VMS_SITE_ID, VMS_LOG_LEVEL) para no duplicar valores en el .env.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vms.core.naming import ID_PATTERN
from vms.core.paths import default_data_dir
from vms.core.settings import env_files

CENTRAL_DIR_NAME = "central"


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class CentralSettings(BaseSettings):
    """Servidor central multi-sede."""

    model_config = SettingsConfigDict(env_prefix="VMS_CENTRAL_", extra="ignore",
                                      env_file_encoding="utf-8", env_ignore_empty=True,
                                      populate_by_name=True)

    data_dir: Path | None = Field(None, description="Carpeta de datos del panel (usuarios, tokens, logs)")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        "INFO", validation_alias=_alias("VMS_CENTRAL_LOG_LEVEL", "VMS_LOG_LEVEL"))
    http_host: str = "0.0.0.0"
    http_port: int = Field(8700, ge=1, le=65535)
    session_hours: int = Field(12, ge=1, le=24 * 30)
    secure_cookies: bool = Field(False, description="Marca la cookie como Secure (actívalo detrás de HTTPS)")
    admin_initial_password: SecretStr | None = None
    pg_dsn: SecretStr | None = Field(None, validation_alias=_alias("VMS_CENTRAL_PG_DSN", "VMS_PG_DSN"))
    pg_pool_max: int = Field(10, ge=1, le=100)
    heartbeat_seconds: int = Field(
        60, ge=10, le=3600,
        validation_alias=_alias("VMS_CENTRAL_HEARTBEAT_SECONDS", "VMS_HEARTBEAT_SECONDS"))
    down_after_intervals: int = Field(3, ge=2, le=20, description="Latidos perdidos para dar la sede por caída")
    trusted_proxies: list[str] = Field(default_factory=list,
                                       description="IPs de proxies inversos de confianza (X-Forwarded-For)")

    @property
    def base_dir(self) -> Path:
        return self.data_dir or (default_data_dir() / CENTRAL_DIR_NAME)

    @property
    def users_file(self) -> Path:
        return self.base_dir / "config" / "users.json"

    @property
    def tokens_file(self) -> Path:
        return self.base_dir / "config" / "site_tokens.json"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    def ensure_dirs(self) -> None:
        from vms.core.paths import restrict_permissions

        for p in (self.base_dir / "config", self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self.base_dir / "config")


class AgentSettings(BaseSettings):
    """Agente de latido que corre en cada sede y envía el estado al panel central por HTTPS."""

    model_config = SettingsConfigDict(env_prefix="VMS_AGENT_", extra="ignore",
                                      env_file_encoding="utf-8", env_ignore_empty=True,
                                      populate_by_name=True)

    data_dir: Path | None = Field(None, validation_alias=_alias("VMS_DATA_DIR"))
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        "INFO", validation_alias=_alias("VMS_AGENT_LOG_LEVEL", "VMS_LOG_LEVEL"))
    site_id: str = Field("site-local", pattern=ID_PATTERN, validation_alias=_alias("VMS_SITE_ID"))
    central_url: str | None = Field(None, validation_alias=_alias("VMS_CENTRAL_URL"),
                                    description="URL del panel central, p. ej. https://central.vpn:8700")
    site_token: SecretStr | None = Field(None, validation_alias=_alias("VMS_SITE_TOKEN"))
    backend_url: str = "http://127.0.0.1:8600"
    username: str | None = Field(None, description="Usuario operador del backend para leer /api/status")
    password: SecretStr | None = None
    interval_seconds: int = Field(
        60, ge=10, le=3600,
        validation_alias=_alias("VMS_AGENT_INTERVAL_SECONDS", "VMS_HEARTBEAT_SECONDS"))
    timeout_seconds: float = Field(10.0, ge=1.0, le=120.0)
    verify_tls: bool = True
    ca_file: Path | None = Field(None, description="CA propia para la central (si no es pública)")

    @field_validator("central_url", "backend_url")
    @classmethod
    def _strip_slash(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("La URL debe empezar por http:// o https://")
        return v or None

    @property
    def base_dir(self) -> Path:
        return self.data_dir or default_data_dir()


def _files(explicit: Path | None) -> list[str] | None:
    files = [explicit] if explicit else env_files()
    return [str(f) for f in files] or None


def load_central_settings(env_file: Path | None = None, **overrides: object) -> CentralSettings:
    return CentralSettings(_env_file=_files(env_file), **overrides)  # type: ignore[call-arg]


def load_agent_settings(env_file: Path | None = None, **overrides: object) -> AgentSettings:
    return AgentSettings(_env_file=_files(env_file), **overrides)  # type: ignore[call-arg]


def running_on_windows() -> bool:
    return os.name == "nt"
