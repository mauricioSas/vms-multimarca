"""Interfaces internas entre módulos (contrato en código; ver docs/CONTRATO.md §5).

- vms.vendors implementa DeviceClient + discover().
- vms.engine implementa Engine.
- vms.api solo depende de estos Protocol y DTO, nunca de clases concretas, para poder
  probarse con los dobles de tests/fakes.py mientras los otros módulos se construyen.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .models import RecordingSettings, RetentionSettings, RtspTransport, Vendor


# =========================================================================== fabricantes
class DeviceInfo(BaseModel):
    vendor: Vendor
    kind: Literal["camera", "nvr", "unknown"] = "unknown"
    model: str = ""
    serial: str = ""
    firmware: str = ""
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
