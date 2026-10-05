"""Modelos base del bloque B6 «Operación, IA de verificación y onboarding» (CONTRATO §18).

Son los DTO de la API y de los archivos propios de B6. Los ajustes que viven en config.json
(`HealthSettings`, `NotificationSettings`, `CameraHealthConfig`, `CameraScope`) están en
`vms.core.models` porque config.json es uno solo.

RGPD (ver docs/RGPD-EIPD.md, «Salud de cámara»): ningún modelo de aquí contiene imágenes ni datos de
personas. La única imagen que guarda B6 es la referencia de cada cámara (mediana de ~15 fotogramas,
que borra a los transeúntes) y la última del aviso de sabotaje; se sirven bajo demanda y nunca viajan
en avisos ni en el latido.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vms.core.models import EntityId, utcnow

Severity = Literal["info", "warning", "critical"]


class _Ops(BaseModel):
    model_config = ConfigDict(extra="allow")


# =========================================================================== §18.2 salud de imagen
class HealthCause(StrEnum):
    """Causas estables (salen en la API, en los avisos y en el informe). Texto en `HEALTH_CAUSE_ES`."""

    NO_REFERENCE = "no_reference"
    NO_SNAPSHOT = "no_snapshot"          # no se pudo obtener imagen (la cámara no responde)
    BLACK = "black"
    COVERED = "covered"
    BLURRED = "blurred"
    MOVED = "moved"
    ROTATED = "rotated"
    SCENE_CHANGED = "scene_changed"
    FROZEN = "frozen"
    IR_STUCK = "ir_stuck"                # filtro IR atascado (color de noche o gris de día)
    IR_WEAK = "ir_weak"                  # LED IR fallando
    DEGRADED = "degraded"                # suciedad, niebla, telaraña: «calidad degradada»
    BACKLIGHT = "backlight"
    COLOR_CAST = "color_cast"
    NOISE = "noise"
    CAMERA_EVENT = "camera_event"        # sabotaje detectado por la propia cámara (v2.1: eventos del NVR)


HEALTH_CAUSE_ES: dict[str, str] = {
    "no_reference": "Falta fijar la imagen de referencia",
    "no_snapshot": "No se pudo obtener imagen de la cámara",
    "black": "Imagen negra",
    "covered": "Cámara tapada",
    "blurred": "Imagen desenfocada",
    "moved": "Cámara movida",
    "rotated": "Cámara girada",
    "scene_changed": "La cámara mira a otro sitio",
    "frozen": "Imagen congelada",
    "ir_stuck": "El filtro de infrarrojos parece atascado",
    "ir_weak": "La iluminación infrarroja parece fallar",
    "degraded": "Calidad de imagen degradada (suciedad, niebla o telarañas)",
    "backlight": "Contraluz o imagen quemada",
    "color_cast": "Dominante de color",
    "noise": "Mucho ruido en la imagen",
    "camera_event": "La propia cámara avisó de sabotaje",
}


class HealthMetrics(_Ops):
    """Medidas de una comprobación (todas relativas a la referencia salvo luma/porcentajes)."""

    luma_mean: float | None = None
    dark_ratio: float | None = None          # % de píxeles < 8
    bright_ratio: float | None = None        # % de píxeles > 250
    std: float | None = None
    entropy: float | None = None
    sharpness_rel: float | None = None       # var(Laplaciano) / la de la referencia
    edges_kept: float | None = None          # bordes de la referencia que siguen
    inliers_ratio: float | None = None       # ORB + RANSAC
    shift_px: float | None = None
    rotation_deg: float | None = None
    color_delta_ab: float | None = None
    saturation_mean: float | None = None
    frame_diff: float | None = None


class HealthCheck(_Ops):
    camera_id: EntityId
    at: datetime = Field(default_factory=utcnow)
    reference: Literal["day", "night", "none"] = "none"
    score: int | None = Field(None, ge=0, le=100)   # None = sin referencia o sin imagen
    status: Literal["ok", "warning", "critical", "unknown"] = "unknown"
    causes: list[HealthCause] = Field(default_factory=list)
    metrics: HealthMetrics = Field(default_factory=HealthMetrics)
    duration_ms: float = 0.0


class CameraHealth(_Ops):
    """GET /api/camera-health[/{id}]: último estado consolidado (con histéresis) de una cámara."""

    camera_id: EntityId
    score: int | None = None
    status: Literal["ok", "warning", "critical", "unknown"] = "unknown"
    causes: list[HealthCause] = Field(default_factory=list)
    since: datetime | None = None            # desde cuándo está en este estado
    last_check: HealthCheck | None = None
    references: dict[Literal["day", "night"], datetime] = Field(default_factory=dict)  # fecha de cada referencia
    clock: ClockCheck | None = None


# =========================================================================== §18.3 hora
class ClockCheck(_Ops):
    camera_id: EntityId | None = None        # None = el propio PC (w32tm/NTP)
    device_id: EntityId | None = None
    at: datetime = Field(default_factory=utcnow)
    skew_s: float | None = None              # + adelantada, − atrasada; None = no se pudo leer
    round_trip_ms: float | None = None
    time_mode: Literal["ntp", "manual", "unknown"] = "unknown"
    status: Literal["ok", "warning", "critical", "unknown"] = "unknown"
    message_es: str = ""


# =========================================================================== §18.4 previsión de grabación
class CameraForecast(_Ops):
    camera_id: EntityId
    bytes_per_hour: float                    # medido en las últimas 24 h
    days_on_disk: float                      # días que ya hay grabados


class RetentionForecast(_Ops):
    at: datetime = Field(default_factory=utcnow)
    target_days: int                         # settings.retention.days
    forecast_days: float                     # días que caben con la tasa real
    disk_total: int
    disk_free: int
    reclaimable: int                         # lo que la retención borrará igualmente
    status: Literal["ok", "warning", "critical"] = "ok"
    rgpd_warning: bool = False               # objetivo > 30 días (art. 22.3 LOPDGDD)
    cameras: list[CameraForecast] = Field(default_factory=list)
    message_es: str = ""


# =========================================================================== §18.5 informe de salud
class CameraDayReport(_Ops):
    camera_id: EntityId
    name: str
    online_ratio: float                      # 0..1
    recording_gaps_min: float
    fps_ratio: float | None = None           # fps reales / esperados
    bitrate_ratio: float | None = None
    health_score_min: int | None = None
    health_causes: list[HealthCause] = Field(default_factory=list)
    clock_skew_s: float | None = None
    retention_days_real: float | None = None
    protected_ranges: int = 0


class HealthReport(_Ops):
    """Informe diario por tienda (GET /api/health-report?date=). Agregado en la central por el latido."""

    site_id: str
    date: str                                # AAAA-MM-DD en la zona de la sede
    generated_at: datetime = Field(default_factory=utcnow)
    status: Literal["ok", "warning", "critical"]
    problems: list[str] = Field(default_factory=list)   # frases en español, ordenadas por gravedad
    cameras: list[CameraDayReport] = Field(default_factory=list)
    disk: dict[str, float] = Field(default_factory=dict)  # percent, free_gb, smart_ok (si se sabe)
    retention: RetentionForecast | None = None
    pc_clock: ClockCheck | None = None


# =========================================================================== §18.6 marcadores
class Bookmark(_Ops):
    id: str = Field(pattern=r"^bm-[0-9a-f]{8}$")
    camera_id: EntityId
    start: datetime
    end: datetime | None = None              # None = punto
    note: str = Field("", max_length=500)
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    protected: bool = False                  # «Proteger»: los segmentos se copian a evidencias/ y la retención no los borra
    protect_reason: str = Field("", max_length=300)
    protect_until: datetime | None = None    # por defecto +90 días
    case_ref: str = Field("", max_length=64) # número de caso o atestado
    protected_by: str = ""
    protected_files: list[str] = Field(default_factory=list)   # nombres de segmento copiados o enlazados
    released_at: datetime | None = None      # cuándo dejó de estar protegido (caducidad o a mano)
    release_reason: str = ""


class BookmarkCreate(BaseModel):
    camera_id: EntityId
    start: datetime
    end: datetime | None = None
    note: str = Field("", max_length=500)
    protect: bool = False
    protect_reason: str = Field("", max_length=300)
    protect_days: int = Field(90, ge=1, le=3650)
    case_ref: str = Field("", max_length=64)


class BookmarkUpdate(BaseModel):
    """PATCH /api/bookmarks/{id} (administrador; se audita). Solo se aplican los campos presentes."""

    note: str | None = Field(None, max_length=500)
    case_ref: str | None = Field(None, max_length=64)
    protect: bool | None = None               # True = proteger (o ampliar); False = dejar de proteger
    protect_reason: str | None = Field(None, max_length=300)
    protect_days: int | None = Field(None, ge=1, le=3650)


# =========================================================================== §18.7 exportación de evidencias
class EvidenceExportRequest(BaseModel):
    camera_ids: list[EntityId] = Field(min_length=1, max_length=16)
    start: datetime
    end: datetime
    reason: str = Field(min_length=3, max_length=500)      # obligatorio
    case_ref: str = Field("", max_length=64)
    recipient: str = Field("", max_length=120)             # quién lo recibe (acta)
    include_mp4: bool = True                                # además de los fMP4 originales


class EvidenceFile(BaseModel):
    path: str                                 # relativo a la raíz del paquete, con «/»
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int
    camera_id: EntityId | None = None
    kind: Literal["segment", "mp4", "viewer", "report", "key", "other"] = "other"


class EvidenceManifest(BaseModel):
    """manifiesto.json del paquete (formato estable, `schema` 1). Lo firma `manifiesto.sig` (Ed25519)."""

    schema_: Literal[1] = Field(1, alias="schema")
    product: Literal["vms-multimarca"] = "vms-multimarca"
    product_version: str
    export_id: str
    created_at: datetime
    created_by: str
    reason: str
    case_ref: str = ""
    recipient: str = ""
    site: dict[str, str]                      # id, name, code, timezone
    range_utc: tuple[datetime, datetime]
    range_local: tuple[str, str]              # en la zona de la sede, ISO con desfase
    cameras: list[dict[str, object]]          # camera_id, name, device, codec, clock_skew_s medido
    files: list[EvidenceFile]
    signing_key: dict[str, str]               # algorithm "ed25519", public_key (base64), key_id (sha256 de la pública)
    pc_clock: dict[str, object] = Field(default_factory=dict)   # última medida de la hora del PC (si la hay)
    notes_es: list[str] = Field(default_factory=list)          # avisos (p. ej. «no se pudo unir el MP4»)
    model_config = ConfigDict(populate_by_name=True)


class EvidenceExport(_Ops):
    export_id: str = Field(pattern=r"^ev-[0-9]{8}-[0-9a-f]{6}$")
    state: Literal["queued", "running", "done", "failed"] = "queued"
    progress: float = 0.0
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    request: EvidenceExportRequest
    download_url: str | None = None
    bytes: int | None = None
    error: str = ""
    sha256_manifest: str | None = None
    files: int | None = None
    finished_at: datetime | None = None


# =========================================================================== §18.10 avisos
class NotificationRecord(_Ops):
    id: str
    at: datetime = Field(default_factory=utcnow)
    kind: str                                 # camera_down, tamper, disk, recording_gap, clock_skew, …
    severity: Severity
    channel: Literal["email", "webhook", "telegram"]
    grouped: int = 1
    title_es: str
    ok: bool
    error: str = ""


# =========================================================================== §18.11 ¿por qué no conecta?
class DiagnosisStep(BaseModel):
    code: Literal["ping", "tcp_http", "tcp_rtsp", "http_response", "auth", "lockout", "rtsp_describe", "codec",
                  "clock", "engine", "path_ready"]
    ok: bool | None                           # None = no se pudo comprobar (paso anterior falló)
    detail_es: str
    action_es: str = ""                       # qué hacer, en lenguaje de tienda
    elapsed_ms: float = 0.0


class DiagnosisResult(BaseModel):
    device_id: EntityId | None = None
    camera_id: EntityId | None = None
    at: datetime = Field(default_factory=utcnow)
    steps: list[DiagnosisStep]
    probable_cause_es: str
    summary_es: str                           # redactado por reglas; el LLM opcional solo lo reescribe
    llm_used: bool = False


# =========================================================================== §18.12 auditoría de seguridad
class Advisory(BaseModel):
    """Una fila de la tabla de avisos propia (`advisories.json`, CONTRATO §18.12)."""

    id: str = Field(pattern=r"^ADV-[0-9]{4}-[0-9]{3}$")
    vendor: str                               # id de driver: hikvision, dahua…
    model_regex: str                          # expresión regular sobre DeviceInfo.model
    families: list[str] = Field(default_factory=list)   # IPC_G3, IPC_H5…
    compare: Literal["build_date", "version"]
    fixed_build_date: str | None = None       # «2021-06-28» (Hik: build 210628 o posterior corrige)
    fixed_version: str | None = None          # «2.820.0000000.18.R.210705» (Dahua)
    cves: list[str] = Field(min_length=1)
    kev: bool = False                         # en el catálogo KEV de CISA
    kev_date_added: str | None = None
    cvss: float | None = None
    epss: float | None = None
    vendor_advisory_url: str = ""
    reviewed: str                             # fecha de la última revisión humana (AAAA-MM-DD)
    notes_es: str = ""


class AdvisoryTable(BaseModel):
    schema_: Literal[1] = Field(1, alias="schema")
    generated_at: datetime
    source: str = "Unmanned Studio"
    kev_catalog_version: str = ""
    nvd_notice: str = "This product uses the NVD API but is not endorsed or certified by the NVD."
    advisories: list[Advisory]
    model_config = ConfigDict(populate_by_name=True)


class SecurityFinding(BaseModel):
    device_id: EntityId
    check: Literal["weak_password", "admin_user", "anonymous_rtsp", "anonymous_onvif", "telnet", "ssh",
                   "http_no_tls", "sdk_port", "upnp", "p2p_cloud", "firmware_cve", "clock"]
    severity: Severity
    status: Literal["vulnerable", "probably_vulnerable", "ok", "unknown"]
    detail_es: str
    advisory_ids: list[str] = Field(default_factory=list)
    action_es: str = ""


class SecurityAuditReport(BaseModel):
    site_id: str
    at: datetime = Field(default_factory=utcnow)
    advisories_version: str
    findings: list[SecurityFinding]
    devices_checked: int
    admin_credentials_used: bool = False      # se usaron credenciales de administrador temporales (no se guardan)


# =========================================================================== §18.13 línea de tiempo
class TimelineEvent(BaseModel):
    camera_id: EntityId
    layer: Literal["recording_gap", "bookmark", "health", "clock", "analytics_alert", "nvr_event", "protected"]
    start: datetime
    end: datetime | None = None
    severity: Severity = "info"
    title_es: str
    ref_id: str | None = None                 # id del marcador, alerta, etc.


# =========================================================================== §18.14 onboarding
class OnboardingState(BaseModel):
    username: str
    wizard_completed: bool = False
    wizard_step: int = 0
    tours_seen: list[str] = Field(default_factory=list)    # ids de recorrido: «panel», «reproduccion»…
    dismissed_hints: list[str] = Field(default_factory=list)


CameraHealth.model_rebuild()
