"""Modelo de datos persistente (config.json / users.json) y modelos de la API.

Convenciones:
- Identificadores: ver vms.core.naming (p. ej. «dev-1a2b3c4d», «cam-9f8e7d6c»).
- Fechas: datetime con zona (UTC) en ISO 8601.
- Coordenadas de analítica: normalizadas 0..1 sobre la imagen (x a la derecha, y hacia abajo),
  independientes de la resolución del flujo.
- Las contraseñas de equipos NUNCA forman parte de estos modelos persistentes: van al
  almacén de credenciales (vms.core.credentials). Las de usuarios se guardan como hash argon2.

Compatibilidad entre versiones (v2, CONTRATO §13.7 y PLAN-V2 §2.8):
- Los modelos PERSISTENTES (lo que se guarda en config.json y users.json) usan `extra="allow"`: una
  versión N-1 (por ejemplo tras un rollback) conserva los campos que no conoce en vez de borrarlos.
- Los modelos de PETICIÓN (…Create, …Update, …Request) siguen con `extra="ignore"`. Los cuerpos que
  llegan como dict se filtran con `known_fields_only()` antes de mezclarse con lo guardado.
- `Vendor` es un identificador de driver (texto) y no una lista cerrada: el alta lo valida contra el
  registro de drivers (`known_vendor_ids()`), pero un equipo guardado con una marca que esta versión
  no conoce se conserva («Driver no disponible en esta versión»).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (BaseModel, ConfigDict, Field, SecretStr, field_validator,
                      model_validator)

from . import rtsp
from .naming import ID_PATTERN, new_id

VENDOR_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"
Vendor = Annotated[str, Field(pattern=VENDOR_ID_PATTERN)]
# «dvr» y «xvr» (grabadores analógicos/híbridos) entran en la v2: aditivo, la interfaz v1 solo ofrece camera/nvr.
DeviceKind = Literal["camera", "nvr", "dvr", "xvr"]
Role = Literal["admin", "operator"]
GridSize = Literal[1, 4, 9, 16]
StreamKind = Literal["main", "sub"]
RtspTransport = Literal["tcp", "udp", "automatic"]
DetectorModel = Literal["rfdetr-nano", "rfdetr-small", "rfdetr-medium", "rfdetr-base"]

MAX_MONITORS = 4
WALL_SLOTS = 16
EntityId = Annotated[str, Field(pattern=ID_PATTERN)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Marcas que esta versión sabe manejar. El registro de drivers (vms/vendors/registry.py, B5) las
# amplía con register_vendor_ids() al importarse; estas cuatro son las de la v1.
_KNOWN_VENDORS: set[str] = {"hikvision", "dahua", "onvif", "generic"}


def register_vendor_ids(ids: Iterable[str]) -> None:
    """Lo llama el registro de drivers al cargarse (aditivo)."""
    _KNOWN_VENDORS.update(ids)


def known_vendor_ids() -> frozenset[str]:
    return frozenset(_KNOWN_VENDORS)


def _check_known_vendor(v: str) -> str:
    if v not in _KNOWN_VENDORS:
        raise ValueError("Marca no disponible en esta versión del programa")
    return v


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class _Persisted(_Model):
    """Modelo que se guarda en disco: conserva los campos desconocidos (escritos por otra versión)."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)


def _model_in(annotation: Any) -> type[BaseModel] | None:
    """El BaseModel que hay dentro de una anotación (X, X | None, Annotated[X, ...]), si lo hay."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin is Annotated:
        return _model_in(get_args(annotation)[0])
    if origin in (Union, UnionType):
        found = [m for m in (_model_in(a) for a in get_args(annotation)) if m is not None]
        return found[0] if len(found) == 1 else None
    return None


def known_fields_only(model: type[BaseModel], data: Any) -> Any:
    """Quita de un cuerpo de petición (dict) las claves que `model` no conoce, también en submodelos.

    Con `extra="allow"` en los modelos persistentes, un cuerpo con claves inventadas acabaría en
    config.json; se filtra antes de mezclarlo con lo guardado (que sí conserva lo de otras versiones)."""
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            continue
        sub = _model_in(field.annotation)
        out[key] = known_fields_only(sub, value) if sub is not None else value
    return out


# --------------------------------------------------------------------------- sede
class Site(_Persisted):
    id: EntityId = "site-local"
    name: str = Field("Sede", min_length=1, max_length=80)
    code: str = Field("", max_length=32, description="Código interno de tienda del cliente")
    timezone: str = "Europe/Madrid"


# --------------------------------------------------------------------------- equipos
def _check_host(v: str) -> str:
    v = (v or "").strip()
    if "://" in v or "/" in v:
        raise ValueError("Escribe solo la IP o el nombre del equipo, sin rtsp:// ni rutas")
    if not rtsp.is_valid_host(v):
        raise ValueError("La IP o el nombre DNS no es válido")
    return v


class DeviceBase(_Model):
    name: str = Field(min_length=1, max_length=80)
    vendor: Vendor
    kind: DeviceKind = "camera"
    host: str
    rtsp_port: int = Field(554, ge=1, le=65535)
    http_port: int = Field(80, ge=1, le=65535)
    https: bool = False
    onvif_port: int | None = Field(None, ge=1, le=65535, description="None = mismo que http_port")
    username: str = Field("", max_length=64)
    enabled: bool = True
    notes: str = Field("", max_length=500)
    # v2 (CONTRATO §16.3): permitir autenticación Basic con este equipo (por defecto no: Digest).
    allow_basic: bool = False
    # v2: seguir al equipo si cambia de IP (misma serie o MAC en el descubrimiento). Por defecto no:
    # la interfaz propone el cambio y el administrador confirma (PLAN-V2 §3.2 punto 11).
    follow_ip: bool = False

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return _check_host(v)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        if ":" in v:
            raise ValueError("El usuario no puede contener «:»")
        return v.strip()


class DeviceIdentity(_Persisted):
    """Identidad estable del equipo para encontrarlo si cambia de IP (CONTRATO §16.3)."""

    serial: str = Field("", max_length=64)
    mac: str = Field("", max_length=32, description="aa:bb:cc:dd:ee:ff en minúsculas")
    source: Literal["api", "onvif", "wsd", "sadp", "dhip", "manual", ""] = ""
    seen_at: datetime | None = None


class Device(DeviceBase):
    """Equipo físico (cámara IP o NVR). Las credenciales se guardan aparte, por id."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    id: EntityId = Field(default_factory=lambda: new_id("dev"))
    model: str = ""
    serial: str = ""
    firmware: str = ""
    identity: DeviceIdentity | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="before")
    @classmethod
    def _drop_plaintext_password(cls, data: Any) -> Any:
        # Una contraseña en claro dentro de config.json (configuraciones antiguas o editadas a mano)
        # nunca se conserva, aunque el modelo admita campos desconocidos. ConfigStore avisa de ello.
        if isinstance(data, dict) and "password" in data:
            data = {k: v for k, v in data.items() if k != "password"}
        return data


class DeviceCreate(DeviceBase):
    password: SecretStr | None = None
    import_channels: list[int] | Literal["all"] | None = Field(
        None, description="Canales a dar de alta como cámaras tras crear el equipo")

    @field_validator("vendor")
    @classmethod
    def _vendor_known(cls, v: str) -> str:
        return _check_known_vendor(v)


class DeviceUpdate(_Model):
    """PATCH: solo se aplican los campos presentes. password: None = sin cambios, "" = borrar."""

    name: str | None = Field(None, min_length=1, max_length=80)
    vendor: Vendor | None = None
    kind: DeviceKind | None = None
    host: str | None = None
    rtsp_port: int | None = Field(None, ge=1, le=65535)
    http_port: int | None = Field(None, ge=1, le=65535)
    https: bool | None = None
    onvif_port: int | None = Field(None, ge=1, le=65535)
    username: str | None = Field(None, max_length=64)
    password: SecretStr | None = None
    enabled: bool | None = None
    notes: str | None = Field(None, max_length=500)
    allow_basic: bool | None = None
    follow_ip: bool | None = None

    @field_validator("host")
    @classmethod
    def _host(cls, v: str | None) -> str | None:
        return None if v is None else _check_host(v)

    @field_validator("vendor")
    @classmethod
    def _vendor_known(cls, v: str | None) -> str | None:
        return None if v is None else _check_known_vendor(v)


class DeviceTestRequest(DeviceBase):
    """Prueba de conexión de un equipo aún no guardado."""

    name: str = "prueba"
    password: SecretStr | None = None

    @field_validator("vendor")
    @classmethod
    def _vendor_known(cls, v: str) -> str:
        return _check_known_vendor(v)


# --------------------------------------------------------------------------- cámaras
def _check_rtsp_path(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    if " " in v or "://" in v:
        raise ValueError("La ruta RTSP no puede contener espacios ni rtsp://; solo la parte tras el puerto")
    return rtsp.normalize_path(v)


class CameraBase(_Model):
    name: str = Field(min_length=1, max_length=80)
    device_id: EntityId
    channel: int = Field(1, ge=1, le=512)
    enabled: bool = True
    record: bool = True
    main_path: str | None = Field(None, description="Ruta RTSP manual; None = preset del fabricante")
    sub_path: str | None = Field(None, description="Ruta manual del subflujo; None = preset")
    has_sub: bool = True
    rtsp_transport: RtspTransport = "tcp"

    @field_validator("main_path", "sub_path")
    @classmethod
    def _paths(cls, v: str | None) -> str | None:
        return _check_rtsp_path(v)


class CameraHealthConfig(_Persisted):
    """Ajustes de «salud de imagen» de una cámara (CONTRATO §18.2). Sin imágenes: solo geometría."""

    enabled: bool = True
    # Zonas que no se comparan con la referencia (puertas automáticas, pantallas, reloj del OSD):
    # polígonos normalizados 0..1 como las reglas de analítica.
    masks: list[list[tuple[float, float]]] = Field(default_factory=list, max_length=16)


class Camera(CameraBase):
    """Canal de vídeo (una cámara IP = canal 1; un NVR = un canal por cámara)."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    health: CameraHealthConfig = Field(default_factory=CameraHealthConfig)
    id: EntityId = Field(default_factory=lambda: new_id("cam"))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CameraCreate(CameraBase):
    pass


class CameraUpdate(_Model):
    name: str | None = Field(None, min_length=1, max_length=80)
    channel: int | None = Field(None, ge=1, le=512)
    enabled: bool | None = None
    record: bool | None = None
    main_path: str | None = None
    sub_path: str | None = None
    has_sub: bool | None = None
    rtsp_transport: RtspTransport | None = None

    @field_validator("main_path", "sub_path")
    @classmethod
    def _paths(cls, v: str | None) -> str | None:
        return _check_rtsp_path(v)


# --------------------------------------------------------------------------- muros
def _empty_cells() -> list[str | None]:
    return [None] * WALL_SLOTS


class WallLayout(_Persisted):
    """Disposición de un monitor. Siempre 16 celdas para no perder asignaciones al cambiar grid."""

    monitor: int = Field(ge=1, le=MAX_MONITORS)
    name: str = Field("", max_length=40)
    grid: GridSize = 4
    cells: list[EntityId | None] = Field(default_factory=lambda: _empty_cells())

    @field_validator("cells", mode="before")
    @classmethod
    def _pad(cls, v: object) -> object:
        if isinstance(v, list):
            v = list(v)[:WALL_SLOTS]
            v += [None] * (WALL_SLOTS - len(v))
        return v

    def visible_cells(self) -> list[str | None]:
        return list(self.cells[: self.grid])


class WallUpdate(_Model):
    name: str | None = Field(None, max_length=40)
    grid: GridSize | None = None
    cells: list[EntityId | None] | None = Field(None, max_length=WALL_SLOTS)


# --------------------------------------------------------------------------- usuarios
USERNAME_RE = re.compile(r"^[A-Za-z0-9._\-]{3,32}$")


class CameraScope(_Persisted):
    """Permisos por cámara de un operador (CONTRATO §18.8). `None` en el usuario = todas (como en la v1).

    Los administradores no tienen ámbito: ven y hacen todo."""

    cameras: list[EntityId] = Field(default_factory=list, description="Cámaras que puede ver")
    live: bool = True
    playback: bool = True
    export: bool = False
    bookmark: bool = True


class UserPublic(_Persisted):
    username: str
    role: Role
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None
    camera_scope: CameraScope | None = None


class User(UserPublic):
    password_hash: str

    def public(self) -> UserPublic:
        return UserPublic(**self.model_dump(exclude={"password_hash"}))


class UserCreate(_Model):
    username: str
    password: SecretStr
    role: Role = "operator"

    @field_validator("username")
    @classmethod
    def _u(cls, v: str) -> str:
        if not USERNAME_RE.match(v):
            raise ValueError("Usuario: 3 a 32 caracteres (letras, números, punto, guion o guion bajo)")
        return v

    @field_validator("password")
    @classmethod
    def _p(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres")
        return v


class UserUpdate(_Model):
    password: SecretStr | None = None
    role: Role | None = None
    enabled: bool | None = None

    @field_validator("password")
    @classmethod
    def _p(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres")
        return v


# --------------------------------------------------------------------------- analítica
NormPoint = Annotated[tuple[float, float], Field(description="[x, y] normalizado 0..1")]


def _check_point(p: tuple[float, float]) -> tuple[float, float]:
    x, y = p
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise ValueError("Las coordenadas deben estar entre 0 y 1")
    return (float(x), float(y))


class _RuleBase(_Persisted):
    id: EntityId = Field(default_factory=lambda: new_id("rule"))
    camera_id: EntityId
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    updated_at: datetime = Field(default_factory=utcnow)


class LineRule(_RuleBase):
    """Línea de puerta. Cuenta cruces de personas.

    Sentido «entrada» (sin invertir): con v = end - start y el punto p del objeto,
    s(p) = v.x * (p.y - start.y) - v.y * (p.x - start.x)   (coordenadas de imagen, y hacia abajo).
    Un objeto que pasa de s < 0 a s > 0 es una ENTRADA; de s > 0 a s < 0, una SALIDA.
    La flecha de «entrada» que dibuja la interfaz apunta en la dirección n = (-v.y, v.x).
    invert=True intercambia entradas y salidas.
    """

    kind: Literal["line"] = "line"
    start: NormPoint
    end: NormPoint
    invert: bool = False

    @field_validator("start", "end")
    @classmethod
    def _pt(cls, v: tuple[float, float]) -> tuple[float, float]:
        return _check_point(v)

    @model_validator(mode="after")
    def _len(self) -> "LineRule":
        if abs(self.start[0] - self.end[0]) < 1e-3 and abs(self.start[1] - self.end[1]) < 1e-3:
            raise ValueError("La línea es demasiado corta")
        return self


class ZoneRule(_RuleBase):
    """Zona (polígono) de cola de cajas. Mide ocupación y avisa si se supera el umbral."""

    kind: Literal["zone"] = "zone"
    polygon: list[NormPoint] = Field(min_length=3, max_length=32)
    alert_threshold: int = Field(5, ge=1, le=200, description="Personas en zona para avisar")
    alert_min_seconds: int = Field(60, ge=0, le=3600, description="Tiempo seguido por encima del umbral")
    alert_cooldown_seconds: int = Field(600, ge=0, le=86400, description="Mínimo entre dos avisos")
    clear_below: int | None = Field(None, ge=0, description="Fin de alerta por debajo de este valor; None = umbral - 1")

    @field_validator("polygon")
    @classmethod
    def _poly(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [_check_point(p) for p in v]

    def clear_below_problem(self) -> str | None:
        """Texto del error si `clear_below` no es menor que el umbral (la API lo rechaza con 422).

        No es un validador del modelo para que un config.json antiguo con ese valor siga cargando;
        la analítica lo corrige al usarlo (`analytics.alerts`)."""
        if self.clear_below is not None and self.clear_below >= self.alert_threshold:
            return "«Fin de alerta por debajo de» debe ser menor que el umbral de aviso"
        return None


AnalyticsRule = Annotated[Union[LineRule, ZoneRule], Field(discriminator="kind")]


class CameraAnalytics(_Persisted):
    camera_id: EntityId
    enabled: bool = False
    fps: float = Field(2.0, ge=0.5, le=30.0, description="Puerta 10-15, cajas 1-2")
    detector: DetectorModel = "rfdetr-nano"
    confidence: float = Field(0.5, ge=0.1, le=0.95)
    stream: StreamKind = "sub"


# --------------------------------------------------------------------------- ajustes
class RetentionSettings(_Persisted):
    days: int = Field(30, ge=1, le=3650, description="MediaMTX recordDeleteAfter")
    disk_guard_percent: int = Field(
        90, ge=0, le=99,
        description="Si el disco de grabación supera este %, el motor borra lo más antiguo. 0 = desactivado")


class RecordingSettings(_Persisted):
    segment_seconds: int = Field(900, ge=60, le=86400, description="MediaMTX recordSegmentDuration")
    part_seconds: int = Field(1, ge=1, le=10, description="MediaMTX recordPartDuration (pérdida máx. ante corte)")
    recordings_dir: str | None = Field(None, description="None = <datos>/recordings")


class AlertSettings(_Persisted):
    telegram_enabled: bool = False
    telegram_chat_id: str | None = Field(None, max_length=64)


class NotificationRule(_Persisted):
    """Qué avisos salen por qué canal (CONTRATO §18.6). Sin imágenes salvo `attach_snapshot` en sabotaje."""

    kinds: list[str] = Field(default_factory=lambda: ["camera_down", "tamper", "disk", "recording_gap",
                                                      "clock_skew", "retention_forecast", "update_failed"])
    min_severity: Literal["info", "warning", "critical"] = "warning"
    channels: list[Literal["email", "webhook", "telegram"]] = Field(default_factory=list)
    quiet_hours: tuple[str, str] | None = Field(None, description="(«22:00», «07:00») hora local de la sede")
    group_seconds: int = Field(120, ge=0, le=3600, description="Agrupa avisos parecidos en este intervalo")
    attach_snapshot: bool = False


class NotificationSettings(_Persisted):
    """Correo y webhook (CONTRATO §18.6). Las contraseñas y tokens van al almacén de credenciales,
    nunca aquí: `smtp_password` y `webhook_secret` se guardan con CredentialStore bajo `notify:*`."""

    email_enabled: bool = False
    smtp_host: str = Field("", max_length=253)
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_starttls: bool = True
    smtp_username: str = Field("", max_length=128)
    email_from: str = Field("", max_length=254)
    email_to: list[str] = Field(default_factory=list, max_length=20)
    webhook_enabled: bool = False
    webhook_url: str = Field("", max_length=500)
    rules: list[NotificationRule] = Field(default_factory=lambda: [NotificationRule()])


class HealthSettings(_Persisted):
    """Salud de imagen, desfase horario y previsión de grabación (CONTRATO §18.2-§18.4)."""

    enabled: bool = True
    check_interval_s: int = Field(180, ge=60, le=3600, description="Una comprobación por cámara cada N s")
    hysteresis: int = Field(3, ge=1, le=10, description="Comprobaciones seguidas antes de avisar")
    clock_warn_s: float = Field(2.0, ge=0.5, le=600)
    clock_critical_s: float = Field(30.0, ge=1, le=3600)


class SystemSettings(_Persisted):
    site: Site = Field(default_factory=Site)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)


# --------------------------------------------------------------------------- raíz
# 2 = v2.0 (campos nuevos con valor por defecto; migración v1→v2 en vms/core/config_migrations.py).
CONFIG_VERSION = 2


class AppConfig(_Persisted):
    """Contenido completo de config.json."""

    version: int = CONFIG_VERSION
    devices: list[Device] = Field(default_factory=list)
    cameras: list[Camera] = Field(default_factory=list)
    walls: list[WallLayout] = Field(
        default_factory=lambda: [WallLayout(monitor=i) for i in range(1, MAX_MONITORS + 1)])
    analytics_cameras: list[CameraAnalytics] = Field(default_factory=list)
    analytics_rules: list[AnalyticsRule] = Field(default_factory=list)
    settings: SystemSettings = Field(default_factory=SystemSettings)

    # ---- consultas -------------------------------------------------------
    def device(self, device_id: str) -> Device | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def camera(self, camera_id: str) -> Camera | None:
        return next((c for c in self.cameras if c.id == camera_id), None)

    def cameras_of(self, device_id: str) -> list[Camera]:
        return [c for c in self.cameras if c.device_id == device_id]

    def wall(self, monitor: int) -> WallLayout | None:
        return next((w for w in self.walls if w.monitor == monitor), None)

    def rules_of(self, camera_id: str) -> list[LineRule | ZoneRule]:
        return [r for r in self.analytics_rules if r.camera_id == camera_id]

    def analytics_of(self, camera_id: str) -> CameraAnalytics | None:
        return next((a for a in self.analytics_cameras if a.camera_id == camera_id), None)

    # ---- borrado en cascada ---------------------------------------------
    def remove_camera(self, camera_id: str) -> None:
        self.cameras = [c for c in self.cameras if c.id != camera_id]
        for w in self.walls:
            w.cells = [None if c == camera_id else c for c in w.cells]
        self.analytics_rules = [r for r in self.analytics_rules if r.camera_id != camera_id]
        self.analytics_cameras = [a for a in self.analytics_cameras if a.camera_id != camera_id]

    def remove_device(self, device_id: str) -> list[str]:
        """Borra el equipo y sus cámaras. Devuelve los ids de cámara eliminados."""
        removed = [c.id for c in self.cameras_of(device_id)]
        for cid in removed:
            self.remove_camera(cid)
        self.devices = [d for d in self.devices if d.id != device_id]
        return removed

    # ---- coherencia ------------------------------------------------------
    def repair(self) -> list[str]:
        """Corrige referencias rotas y duplicados. Devuelve los avisos (en español)."""
        problems: list[str] = []
        seen: set[str] = set()
        devices = []
        for d in self.devices:
            if d.id in seen:
                problems.append(f"Equipo duplicado descartado: {d.name}")
                continue
            seen.add(d.id)
            devices.append(d)
        self.devices = devices
        cams = []
        cam_ids: set[str] = set()
        for c in self.cameras:
            if c.device_id not in seen:
                problems.append(f"Cámara «{c.name}» sin equipo; descartada")
                continue
            if c.id in cam_ids:
                problems.append(f"Cámara duplicada descartada: {c.name}")
                continue
            cam_ids.add(c.id)
            cams.append(c)
        self.cameras = cams
        walls: dict[int, WallLayout] = {}
        for w in self.walls:
            walls.setdefault(w.monitor, w)
        self.walls = [walls.get(i) or WallLayout(monitor=i) for i in range(1, MAX_MONITORS + 1)]
        for w in self.walls:
            w.cells = [c if c in cam_ids else None for c in w.cells]
        self.analytics_rules = [r for r in self.analytics_rules if r.camera_id in cam_ids]
        self.analytics_cameras = [a for a in self.analytics_cameras if a.camera_id in cam_ids]
        return problems
