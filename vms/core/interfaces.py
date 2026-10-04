"""Interfaces internas entre módulos (contrato en código; ver docs/CONTRATO.md §5).

- vms.vendors implementa DeviceClient + discover().
- vms.engine implementa Engine.
- vms.api solo depende de estos Protocol y DTO, nunca de clases concretas, para poder
  probarse con los dobles de tests/fakes.py mientras los otros módulos se construyen.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .models import DeviceIdentity as DeviceIdentity  # reexportado: CONTRATO §16.3
from .models import DeviceKind, RecordingSettings, RetentionSettings, RtspTransport, Vendor


# =========================================================================== fabricantes
class DeviceInfo(BaseModel):
    vendor: Vendor
    kind: Literal["camera", "nvr", "dvr", "xvr", "unknown"] = "unknown"
    model: str = ""
    serial: str = ""
    firmware: str = ""
    firmware_date: str = ""             # v2: «2021-06-28» si el equipo lo da (Hik «build 210628»); auditoría §18.7
    mac: str = ""
    name: str = ""
    channel_count: int = 0


class ChannelInfo(BaseModel):
    channel: int = Field(ge=1)
    name: str = ""
    online: bool | None = None          # None = el equipo no lo informa
    has_sub: bool = True
    main_codec: str | None = None       # "H.264", "H.265"...
    sub_codec: str | None = None
    main_resolution: str | None = None  # "2560x1440"
    sub_resolution: str | None = None
    ip_address: str | None = None       # IP de la cámara detrás del NVR, si se conoce
    main_path: str | None = None        # solo ONVIF/genérico: ruta RTSP descubierta
    sub_path: str | None = None


class DiscoveredDevice(BaseModel):
    host: str
    http_port: int = 80
    vendor_guess: Vendor = "onvif"
    model: str = ""
    name: str = ""
    mac: str = ""
    xaddrs: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    already_added: bool = False         # lo rellena la API comparando con la configuración


class DeviceTestResult(BaseModel):
    ok: bool
    reachable: bool = False
    auth_ok: bool | None = None
    rtsp_ok: bool | None = None
    info: DeviceInfo | None = None
    channels: list[ChannelInfo] = Field(default_factory=list)
    message: str = ""                   # explicación en español para el usuario


class DeviceTime(BaseModel):
    """Hora del equipo leída por API u ONVIF (CONTRATO §18.3). La implementa cada driver (B5)."""

    device_time: datetime                # con zona; si el equipo no la da, UTC
    measured_at: datetime                # hora del PC (UTC) a mitad del viaje de ida y vuelta
    round_trip_ms: float = 0.0
    time_mode: Literal["ntp", "manual", "unknown"] = "unknown"
    ntp_server: str = ""
    source: Literal["onvif", "isapi", "cgi", "unknown"] = "unknown"

    @property
    def skew_s(self) -> float:
        """Segundos que va adelantado (+) o atrasado (−) el equipo respecto al PC."""
        return (self.device_time - self.measured_at).total_seconds()


@runtime_checkable
class DeviceClient(Protocol):
    """Cliente de un equipo concreto (ISAPI, CGI/RPC2 u ONVIF).

    Errores: lanza vms.core.errors.DeviceUnreachable / DeviceAuthFailed /
    DeviceProtocolError / DeviceUnsupported. Nunca devuelve contraseñas ni las registra.
    """

    vendor: Vendor

    async def probe(self) -> DeviceInfo: ...
    async def list_channels(self) -> list[ChannelInfo]: ...
    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        """JPEG en memoria. NUNCA se escribe en disco (RGPD)."""
        ...
    async def aclose(self) -> None: ...


@runtime_checkable
class DeviceClockClient(Protocol):
    """Capacidad opcional `TIME_READ` (v2, B5 la implementa; B6 la usa para el desfase horario)."""

    async def device_time(self) -> DeviceTime: ...


class DeviceSecuritySettings(BaseModel):
    """Ajustes de seguridad leídos con credenciales de administrador TEMPORALES (CONTRATO §18.12).

    None = el equipo no lo informa. Las credenciales nunca se guardan ni se registran."""

    telnet_enabled: bool | None = None
    ssh_enabled: bool | None = None
    upnp_enabled: bool | None = None
    p2p_cloud_enabled: bool | None = None      # Hik-Connect/EZVIZ, Dahua P2P (T2UServer)
    http_enabled: bool | None = None
    https_enabled: bool | None = None
    sdk_port_open: bool | None = None          # 8000 (Hik) / 37777 (Dahua)
    anonymous_onvif: bool | None = None
    raw: dict[str, str] = Field(default_factory=dict)   # valores leídos, para el informe (sin secretos)


@runtime_checkable
class DeviceSecurityClient(Protocol):
    """Capacidad opcional `SECURITY_READ` (v2, B5 la implementa; B6 la usa en la auditoría)."""

    async def security_settings(self, admin_username: str, admin_password: str) -> DeviceSecuritySettings: ...


# =========================================================================== registro de drivers (v2)
class Capability(StrEnum):
    """Qué sabe hacer un driver (CONTRATO §16.1). Los valores son estables (salen en GET /api/vendors)."""

    API_PROBE = "api_probe"            # leer modelo, serie, firmware
    API_CHANNELS = "api_channels"      # listar canales de NVR/DVR/XVR
    API_SNAPSHOT = "api_snapshot"
    API_CODEC_FIX = "api_codec_fix"    # poner el subflujo en H.264 con un clic (Hik y Dahua)
    ONVIF = "onvif"
    ONVIF_MEDIA2 = "onvif_media2"      # Profile T: GetStreamUri de Media2 (H.265 por ONVIF)
    DISCOVERY_WSD = "discovery_wsd"
    DISCOVERY_SADP = "discovery_sadp"
    DISCOVERY_DHIP = "discovery_dhip"
    TIME_READ = "time_read"            # v2 (B6): hora y modo NTP del equipo
    SECURITY_READ = "security_read"    # v2 (B6): ajustes de seguridad con credenciales de administrador temporales
    RTSPS = "rtsps"                    # v2.1
    MJPEG_HTTP = "mjpeg_http"          # v2.1
    EVENTS = "events"                  # fuera de la v2
    PTZ = "ptz"                        # fuera de la v2


Maturity = Literal["verified", "fixtures", "community", "experimental"]
AuthScheme = Literal["digest-sha256", "digest-md5", "basic"]


@dataclass(frozen=True)
class StreamPreset:
    main: str                           # "/Streaming/Channels/{ch}01"
    sub: str | None                     # "/Streaming/Channels/{ch}02"
    third: str | None = None
    rtsp_port: int = 554
    scheme: Literal["rtsp", "rtsps"] = "rtsp"
    query_safe: bool = True             # False si la ruta usa ?query (MediaMTX la descarta en rutas propias)


@dataclass(frozen=True)
class LockoutPolicy:
    attempts: int                       # intentos fallidos antes de bloquear el usuario
    minutes: int                        # minutos de bloqueo


class DetectionHints(BaseModel):
    """Pistas para adivinar la marca de un equipo (descubrimiento, cabeceras HTTP, modelo)."""

    scopes: list[str] = Field(default_factory=list)       # WS-Discovery
    model: str = ""
    name: str = ""
    manufacturer: str = ""
    http_server: str = ""                                  # cabecera Server de la web del equipo
    sadp: bool = False
    dhip: bool = False
    mac: str = ""


@dataclass(frozen=True)
class DriverSpec:
    """Descripción de un driver de fabricante (CONTRATO §16.1). Se registra en vms/vendors/drivers/<id>.py."""

    id: str                                       # "hikvision", "dahua", "uniview", "tplink-vigi", …
    name: str                                     # "Hikvision"
    brands: tuple[str, ...]                       # ("Hikvision", "HiLook", "HiWatch", "LTS", "Annke")
    kinds: tuple[DeviceKind, ...]
    capabilities: frozenset[Capability]
    default_ports: dict[str, int]                 # {"http": 80, "rtsp": 554, "onvif": 80}
    auth: tuple[AuthScheme, ...]
    maturity: Maturity
    detect: Callable[[DetectionHints], float]     # 0..1
    lockout: LockoutPolicy | None = None
    presets: Callable[[int, DeviceKind], StreamPreset] | None = None
    client: Callable[..., DeviceClient] | None = None   # None = solo RTSP/ONVIF
    notes_es: tuple[str, ...] = ()                # avisos para el instalador («Desactiva Smart Coding…»)
    setup_hints_es: tuple[str, ...] = ()          # pasos previos («Activa RTSP en la app Ezviz…»)
    extra: dict[str, str] = field(default_factory=dict)

    def public(self) -> DriverPublic:
        return DriverPublic(
            id=self.id, name=self.name, brands=list(self.brands), kinds=list(self.kinds),
            capabilities=sorted(c.value for c in self.capabilities), default_ports=dict(self.default_ports),
            auth=list(self.auth), maturity=self.maturity,
            lockout=None if self.lockout is None else {"attempts": self.lockout.attempts,
                                                       "minutes": self.lockout.minutes},
            manual_path=self.presets is None and self.client is None,
            notes_es=list(self.notes_es), setup_hints_es=list(self.setup_hints_es))


class DriverPublic(BaseModel):
    """Lo que devuelve GET /api/vendors por driver (sin funciones). CONTRATO §16.4."""

    id: str
    name: str
    brands: list[str]
    kinds: list[DeviceKind]
    capabilities: list[str]
    default_ports: dict[str, int]
    auth: list[AuthScheme]
    maturity: Maturity
    lockout: dict[str, int] | None = None
    manual_path: bool = False
    notes_es: list[str] = Field(default_factory=list)
    setup_hints_es: list[str] = Field(default_factory=list)


# =========================================================================== motor
class CameraSource(BaseModel):
    """Lo que el motor necesita por cámara. Las URLs llevan credenciales: no registrar."""

    camera_id: str
    name: str
    main_url: str
    sub_url: str | None = None
    record: bool = True
    rtsp_transport: RtspTransport = "tcp"

    def __repr__(self) -> str:  # evita que las URLs con contraseña acaben en trazas
        return f"CameraSource(camera_id={self.camera_id!r}, record={self.record})"

    __str__ = __repr__


class EngineStatus(BaseModel):
    running: bool
    pid: int | None = None
    version: str = ""
    restarts: int = 0
    started_at: datetime | None = None
    api_ok: bool = False
    last_error: str = ""


class PathStatus(BaseModel):
    name: str                       # ruta de MediaMTX, p. ej. «cam-1a2b3c4d/main»
    camera_id: str | None = None
    stream: Literal["main", "sub"] | None = None
    ready: bool = False             # hay vídeo llegando
    source_online: bool = False
    readers: int = 0
    bytes_received: int = 0
    tracks: list[str] = Field(default_factory=list)  # ["H264"], ["H265", "MPEG-4 Audio"]...
    recording: bool = False
    last_error: str = ""


class RecordingSpan(BaseModel):
    start: datetime
    duration: float                 # segundos

    @property
    def end(self) -> datetime:
        from datetime import timedelta
        return self.start + timedelta(seconds=self.duration)


class DiskUsage(BaseModel):
    path: str
    total: int
    used: int
    free: int
    percent: float


@runtime_checkable
class Engine(Protocol):
    """Motor de vídeo (MediaMTX supervisado). Todas las operaciones son idempotentes."""

    async def start(self) -> None:
        """Genera mediamtx.yml, arranca el proceso y espera a que su API responda."""
        ...

    async def stop(self) -> None: ...

    async def apply(self, sources: list[CameraSource], recording: RecordingSettings,
                    retention: RetentionSettings, recordings_dir: str) -> None:
        """Sincroniza las rutas de MediaMTX con la lista (alta, cambio, baja) sin reiniciar."""
        ...

    async def status(self) -> EngineStatus: ...

    async def paths_status(self) -> dict[str, PathStatus]:
        """Estado por ruta, indexado por nombre de ruta («<camera_id>/<stream>»)."""
        ...

    async def list_recordings(self, camera_id: str, start: datetime | None,
                              end: datetime | None) -> list[RecordingSpan]: ...

    def playback_get_url(self, camera_id: str, start: datetime, duration: float,
                         fmt: Literal["fmp4", "mp4"] = "fmp4") -> str:
        """URL interna (127.0.0.1) del /get de MediaMTX; la API la sirve como proxy."""
        ...

    def whep_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        """URL interna WHEP de MediaMTX; la API la sirve como proxy."""
        ...

    def rtsp_read_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        """URL RTSP local de lectura (la usa la analítica)."""
        ...

    async def disk_usage(self) -> DiskUsage: ...
