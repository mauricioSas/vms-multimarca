"""Ajustes de proceso leídos de variables de entorno (prefijo VMS_) y archivos .env.

Orden de carga (lo posterior gana):
  1. valores por defecto de esta clase
  2. <carpeta de instalación>/.env
  3. <carpeta de datos>/.env
  4. el archivo indicado en VMS_ENV_FILE
  5. variables de entorno reales del proceso

Los secretos (contraseñas, tokens, DSN) usan SecretStr: nunca salen en repr() ni en registros.
Los ajustes que el usuario cambia desde la interfaz (retención, sede, alertas...) NO van
aquí sino en config.json (vms.core.models.SystemSettings).
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .atomic import atomic_write_text
from .paths import AppPaths, default_data_dir, install_dir, restrict_permissions


DISABLED_VALUES = frozenset({"off", "none", "disabled", "-"})


class VmsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VMS_", extra="ignore", env_file_encoding="utf-8",
                                      env_ignore_empty=True)

    # --- general -----------------------------------------------------------
    data_dir: Path | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    site_id: str = Field("site-local", pattern=r"^[a-z0-9][a-z0-9\-]{2,39}$")

    # --- backend HTTP --------------------------------------------------------
    http_host: str = "0.0.0.0"
    http_port: int = 8600
    # HTTPS para la LAN (opcional). Con certificado, la web va cifrada en https_port (en http_host) y
    # el HTTP sin cifrar queda SOLO en 127.0.0.1:http_port (kiosco, analítica y agente del mismo PC).
    # Certificado autofirmado: python -m vms tls-cert --host <nombre> --ip <IP de la LAN>
    https_port: int = 8643
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    session_hours: int = Field(12, ge=1, le=24 * 30)
    admin_initial_password: SecretStr | None = None
    kiosk_token: SecretStr | None = None
    kiosk_allow_remote: bool = False
    internal_token: SecretStr | None = None   # analítica ↔ backend; si falta se genera en secrets/

    # --- credenciales de equipos --------------------------------------------
    credential_backend: Literal["auto", "keyring", "file"] = "auto"
    secret_key: SecretStr | None = None       # clave Fernet del almacén cifrado de respaldo

    # --- MediaMTX ------------------------------------------------------------
    mediamtx_bin: Path | None = None
    mtx_rtsp_address: str = "127.0.0.1:8554"
    mtx_webrtc_address: str = "127.0.0.1:8889"
    mtx_webrtc_ice_udp: str = ":8189"
    mtx_webrtc_ice_tcp: str = ":8189"
    mtx_webrtc_additional_hosts: list[str] = Field(default_factory=list)
    mtx_api_address: str = "127.0.0.1:9997"
    mtx_playback_address: str = "127.0.0.1:9996"
    mtx_metrics_address: str = "127.0.0.1:9998"

    # --- PostgreSQL / central ------------------------------------------------
    pg_dsn: SecretStr | None = None
    heartbeat_seconds: int = Field(60, ge=10, le=3600)

    # --- alertas e informes --------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    llm_provider: Literal["anthropic", "none"] = "anthropic"
    llm_model: str = ""
    llm_api_key: SecretStr | None = None

    # ------------------------------------------------------------------------
    @field_validator("mtx_webrtc_ice_udp", "mtx_webrtc_ice_tcp", mode="before")
    @classmethod
    def _address_off(cls, v: object) -> object:
        """`off` (o `none`, `disabled`, `-`) desactiva la dirección.

        Una variable vacía en el .env significa «valor por defecto» (env_ignore_empty), así que
        para apagar, por ejemplo, ICE por TCP hay que escribir VMS_MTX_WEBRTC_ICE_TCP=off.
        """
        if isinstance(v, str) and v.strip().lower() in DISABLED_VALUES:
            return ""
        return v

    @model_validator(mode="after")
    def _tls_pair(self) -> "VmsSettings":
        if (self.tls_cert_file is None) != (self.tls_key_file is None):
            raise ValueError("Para HTTPS hacen falta VMS_TLS_CERT_FILE y VMS_TLS_KEY_FILE (los dos o ninguno)")
        if self.tls_enabled and self.https_port == self.http_port:
            raise ValueError("VMS_HTTPS_PORT debe ser distinto de VMS_HTTP_PORT")
        return self

    @property
    def tls_enabled(self) -> bool:
        return self.tls_cert_file is not None and self.tls_key_file is not None

    @property
    def paths(self) -> AppPaths:
        return AppPaths(self.data_dir or default_data_dir())

    def mtx_rtsp_url(self, path: str) -> str:
        """URL local de lectura de una ruta de MediaMTX (la usa la analítica)."""
        host, port = _split_address(self.mtx_rtsp_address)
        return f"rtsp://{host}:{port}/{path.lstrip('/')}"

    def ensure_internal_token(self) -> str:
        """Devuelve el token interno; si no está en el entorno lo crea en secrets/internal.token."""
        if self.internal_token:
            return self.internal_token.get_secret_value()
        paths = self.paths.ensure()
        f = paths.secrets_dir / "internal.token"
        if f.is_file():
            token = f.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(32)
        try:
            # Creación exclusiva: si backend y analítica arrancan a la vez, ambos acaban con el mismo token.
            with open(f, "x", encoding="utf-8") as fh:
                fh.write(token)
                fh.flush()
                os.fsync(fh.fileno())
        except FileExistsError:
            for _ in range(50):
                existing = f.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
                time.sleep(0.02)
            atomic_write_text(f, token)
        restrict_permissions(f)
        return token


def _split_address(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    if not host or host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return host.strip("[]"), int(port)


def env_files(data_dir: Path | None = None) -> list[Path]:
    candidates = [install_dir() / ".env", (data_dir or default_data_dir()) / ".env"]
    explicit = os.environ.get("VMS_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit))
    return [p for p in candidates if p.is_file()]


def load_settings(env_file: Path | None = None, **overrides: object) -> VmsSettings:
    """Carga los ajustes. `env_file` sustituye a la búsqueda automática (útil en pruebas)."""
    files = [env_file] if env_file else env_files()
    return VmsSettings(_env_file=[str(f) for f in files] or None, **overrides)  # type: ignore[call-arg]
