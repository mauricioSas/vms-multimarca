"""Formatos del actualizador (PLAN-V2 §2.7, CONTRATO §13.4, §13.5, §15.4 y §15.6), validados con pydantic.

Todos los modelos persistentes conservan los campos que no conocen (`extra="allow"`): una versión N-1 del
actualizador nunca borra lo que escribió una N.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .versioning import is_valid

# --------------------------------------------------------------------------- constantes del contrato
COMPONENTS = ("runtime", "app", "engine", "viewer", "models", "updater")
VERSION_COMPONENTS = ("runtime", "app", "engine", "viewer", "models")   # viven en versions\<X>\
# Qué carpetas o archivos de `versions\<X>\` aporta cada componente (los zips no pueden salirse de aquí).
COMPONENT_ROOTS: dict[str, tuple[str, ...]] = {
    "runtime": ("runtime",),
    "app": ("app", "bin", "THIRD_PARTY_NOTICES.txt"),
    "engine": ("engine",),
    "viewer": ("viewer",),
    "models": ("models",),
}
KNOWN_SERVICES = ("VMSEngine", "VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral", "VMSUpdater")

JournalState = Literal["idle", "downloaded", "backed_up", "stopping", "switched", "migrated", "started",
                       "verifying", "good", "rolling_back", "rolled_back"]
# Orden de la actualización (CONTRATO §13.5). `switched` es el punto de compromiso.
FORWARD_STATES: tuple[str, ...] = ("downloaded", "backed_up", "stopping", "switched", "migrated", "started",
                                   "verifying", "good")
ROLLBACK_STATES: tuple[str, ...] = ("rolling_back", "rolled_back")
ALL_STATES: tuple[str, ...] = ("idle",) + FORWARD_STATES + ROLLBACK_STATES
TERMINAL_STATES = ("idle", "good", "rolled_back")

LastResult = Literal["update_ok", "update_failed", "metadata_expired", "clock_skew", "none", "rollback_ok",
                     "no_update", "error", "waiting_window", "reboot_pending", "disk_full", "held", "min_from"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


def _check_target_path(v: str) -> str:
    if not _SAFE_TARGET.match(v) or ".." in v.split("/"):
        raise ValueError(f"ruta de target no válida: {v!r}")
    return v


# --------------------------------------------------------------------------- descriptor de versión (§2.7)
class ComponentRef(_Model):
    version: str = Field(min_length=1, max_length=64)
    target: str
    sha256: str
    length: int = Field(gt=0)
    restart: list[str] = Field(default_factory=list)
    recording_gap: bool = False

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        v = v.lower()
        if not _SHA256.match(v):
            raise ValueError("sha256 debe tener 64 caracteres hexadecimales")
        return v

    @field_validator("target")
    @classmethod
    def _target(cls, v: str) -> str:
        return _check_target_path(v)

    @field_validator("restart")
    @classmethod
    def _restart(cls, v: list[str]) -> list[str]:
        unknown = [s for s in v if s not in KNOWN_SERVICES]
        if unknown:
            raise ValueError(f"servicios desconocidos en restart: {unknown}")
        return v


class AuthenticodePolicy(_Model):
    subject_o: str = Field(min_length=1)
    subject_c: str = Field(min_length=2, max_length=2)
    issuers: list[str] = Field(min_length=1)
    # Autores de terceros cuyos binarios ya vienen firmados y se redistribuyen tal cual (runtime de Python).
    third_party: list[str] = Field(default_factory=lambda: ["Python Software Foundation", "Microsoft Corporation"])


class Requires(_Model):
    windows_build_min: int | None = None
    webview2_min: str | None = None


class ReleaseDescriptor(_Model):
    """`bundles/vms-<X.Y.Z>.json` (esquema 1)."""

    schema_: Literal[1] = Field(alias="schema")
    product: Literal["vms-multimarca"]
    version: str
    min_from: str | None = None
    security: bool = False
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    notes_es: str = ""
    config_schema: int = Field(ge=1)
    db: dict[str, Any] = Field(default_factory=dict)
    requires: Requires = Field(default_factory=Requires)
    authenticode: AuthenticodePolicy | None = None     # None = versión sin firmar (decisión N1, solo desarrollo)
    components: dict[str, ComponentRef]

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not is_valid(v):
            raise ValueError(f"versión no válida: {v!r}")
        return v

    @field_validator("min_from")
    @classmethod
    def _min_from(cls, v: str | None) -> str | None:
        if v is not None and not is_valid(v):
            raise ValueError(f"min_from no válida: {v!r}")
        return v

    @model_validator(mode="after")
    def _components(self) -> "ReleaseDescriptor":
        unknown = [c for c in self.components if c not in COMPONENTS]
        if unknown:
            raise ValueError(f"componentes desconocidos: {unknown}")
        for name, ref in self.components.items():
            if not ref.target.startswith(f"components/{name}/"):
                raise ValueError(f"el target de «{name}» debe estar en components/{name}/")
        missing = [c for c in ("runtime", "app") if c not in self.components]
        if missing:
            raise ValueError(f"faltan componentes obligatorios: {missing}")
        return self

    @property
    def bundle_target(self) -> str:
        return bundle_target(self.version)


def bundle_target(version: str) -> str:
    return f"bundles/vms-{version}.json"


def channel_target(channel: str) -> str:
    return f"channels/{channel}.json"


class ChannelDoc(_Model):
    """`channels/<canal>.json` (esquema 1). Mover o pausar un canal es una publicación firmada."""

    schema_: Literal[1] = Field(alias="schema")
    channel: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    version: str
    paused: bool = False
    updated: datetime | None = None

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not is_valid(v):
            raise ValueError(f"versión no válida: {v!r}")
        return v


class AdvisoryTableLite(_Model):
    """Comprobación mínima del esquema 1 de la tabla de avisos (CONTRATO §18.12).

    La validación completa (`AdvisoryTable`) es de B6 en `vms/ops/security/`; el runtime del actualizador no
    lleva `vms`, así que aquí se comprueba lo imprescindible para no instalar basura."""

    schema_: Literal[1] = Field(alias="schema")
    generated_at: datetime
    advisories: list[dict[str, Any]]

    @field_validator("advisories")
    @classmethod
    def _advisories(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, a in enumerate(v):
            if not isinstance(a.get("id"), str) or not isinstance(a.get("vendor"), str):
                raise ValueError(f"aviso {i} sin id o vendor")
        return v


# --------------------------------------------------------------------------- estado local (§13.4, §13.5)
class UpdaterSlot(_Model):
    slot: Literal["a", "b"] = "a"
    previous_slot: Literal["a", "b"] | None = None
    trial: bool = False
    trial_since_unix: int | None = None


class ActivePointer(_Model):
    """`state\\active.json`. Tiempos en segundos Unix (UTC)."""

    schema_: Literal[1] = Field(default=1, alias="schema")
    active: str
    previous: str | None = None
    trial: bool = False
    trial_since_unix: int | None = None
    updater: UpdaterSlot = Field(default_factory=UpdaterSlot)
    updated_unix: int = 0

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class JournalStep(_Model):
    state: str
    started_unix: int
    done_unix: int | None = None


class Journal(_Model):
    """`state\\journal.json`."""

    schema_: Literal[1] = Field(default=1, alias="schema")
    update_id: str = ""
    kind: Literal["release", "rollback", "updater"] = "release"
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    components: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    state: str = "idle"
    attempt: int = 0
    last_good: str | None = None
    backup: str | None = None
    recording_before: int | None = None
    reason: str = ""
    aborted: bool = False               # revertida ANTES del punto de compromiso (no va a la lista negra)
    steps: list[JournalStep] = Field(default_factory=list)
    error: str = ""

    @field_validator("state")
    @classmethod
    def _state(cls, v: str) -> str:
        if v not in ALL_STATES:
            raise ValueError(f"estado del diario desconocido: {v!r}")
        return v

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    @property
    def current_step(self) -> JournalStep | None:
        return self.steps[-1] if self.steps else None

    @property
    def in_progress(self) -> bool:
        """¿Hay algo que retomar? Un estado final (`good`, `rolled_back`) cuenta como terminado solo cuando
        su paso está marcado como hecho: si el corte llega a mitad de «good», el puntero podría seguir «a
        prueba» y hay que terminarlo."""
        if self.state == "idle":
            return False
        if self.state in TERMINAL_STATES:
            step = self.current_step
            return step is None or step.state != self.state or step.done_unix is None
        return True


class PublicStatus(_Model):
    """`updater\\public-status.json` (legible por Usuarios; nunca lleva secretos)."""

    schema_: Literal[1] = Field(default=1, alias="schema")
    installed: str | None = None
    channel: str = "stable"
    state: str = "idle"
    hold: bool = False
    window: str | None = None
    skipped: list[str] = Field(default_factory=list)   # omitidas tras una vuelta atrás manual
    last_check: str | None = None
    last_result: str = "none"
    message_es: str = ""
    available: str | None = None
    metadata_expires: str | None = None
    clock_skew_s: float | None = None
    reboot_pending: bool = False
    updated: str | None = None
    updater_version: str | None = None
    paused_at: str | None = None          # solo con VMS_UPDATER_PAUSE_AT (pruebas de corte de luz)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class CentralDirective(_Model):
    """Lo que pide el panel central en la respuesta del latido (CONTRATO §15.6)."""

    check: bool = False
    channel: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,31}$")
    hold: bool | None = None
    rollback_to: str | None = None
    window: str | None = None
    unskip_at: str | None = None        # «Permitir de nuevo» la versión omitida tras volver atrás
    received: str | None = None


class LocalUpdaterConfig(_Model):
    """`updater\\updater.json`: ajustes de la sede (los escribe el instalador y el panel por el latido)."""

    channel: str = Field(default="stable", pattern=r"^[a-z][a-z0-9-]{1,31}$")
    hold: bool = False
    window: str = Field(default="01:00-03:00", pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    services: list[str] = Field(default_factory=lambda: [s for s in KNOWN_SERVICES if s != "VMSUpdater"])
    inno_app_id: str | None = None          # GUID del AppId de Inno (B3), para escribir DisplayVersion
    analytics_expected: bool | None = None  # None = lo dice el health check
