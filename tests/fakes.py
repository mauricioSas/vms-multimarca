"""Dobles en memoria de las interfaces internas (vms.core.interfaces).

Permiten probar vms.api sin MediaMTX ni equipos reales. Cumplen los Protocol Engine y
DeviceClient (comprobado en tests/core/test_interfaces.py). Cualquier agente puede
ampliarlos; si cambia un Protocol, se actualizan aquí en el mismo cambio.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from vms.core.errors import DeviceAuthFailed, DeviceUnreachable, EngineUnavailable
from vms.core.interfaces import (CameraSource, ChannelInfo, DeviceInfo, DiskUsage, EngineStatus,
                                 PathStatus, RecordingSpan)
from vms.core.models import RecordingSettings, RetentionSettings, Vendor
from vms.core.naming import mtx_path

SNAPSHOT = (Path(__file__).resolve().parents[1] / "tools" / "mocks" / "assets" / "snapshot.jpg").read_bytes()


SITE_TZ = ZoneInfo("Europe/Madrid")   # zona por defecto de la sede (Site.timezone)


def default_spans(now: datetime, tz: ZoneInfo = SITE_TZ) -> list[RecordingSpan]:
    """Grabaciones simuladas de HOY a mediodía en la zona de la sede (10:00-11:00 y 11:05-11:35).

    Antes eran «hace 2 h en UTC» y, entre las 00:00 y las ~02:00 de Madrid, caían en el día anterior:
    la línea de tiempo de «hoy» salía vacía (prueba dependiente de la hora, PLAN-V2 §5)."""
    local_day = now.astimezone(tz).date()
    base = datetime(local_day.year, local_day.month, local_day.day, 10, 0, tzinfo=tz).astimezone(timezone.utc)
    return [RecordingSpan(start=base, duration=3600.0),
            RecordingSpan(start=base + timedelta(hours=1, minutes=5), duration=1800.0)]


class FakeEngine:
    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.clock = clock
        self.running = False
        self.sources: dict[str, CameraSource] = {}
        self.recording: RecordingSettings | None = None
        self.retention: RetentionSettings | None = None
        self.recordings_dir = ""
        self.apply_calls = 0
        self.offline: set[str] = set()          # camera_id sin vídeo
        self.spans: dict[str, list[RecordingSpan]] = {}

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def apply(self, sources: list[CameraSource], recording: RecordingSettings,
                    retention: RetentionSettings, recordings_dir: str) -> None:
        if not self.running:
            raise EngineUnavailable("El motor de vídeo no está en marcha")
        self.apply_calls += 1
        self.sources = {s.camera_id: s for s in sources}
        self.recording, self.retention, self.recordings_dir = recording, retention, recordings_dir

    async def status(self) -> EngineStatus:
        return EngineStatus(running=self.running, pid=4242 if self.running else None, version="v1.21.1",
                            api_ok=self.running)

    async def paths_status(self) -> dict[str, PathStatus]:
        out: dict[str, PathStatus] = {}
        for cid, src in self.sources.items():
            online = cid not in self.offline
            for stream in ("main", "sub"):
                if stream == "sub" and not src.sub_url:
                    continue
                name = mtx_path(cid, stream)  # type: ignore[arg-type]
                out[name] = PathStatus(name=name, camera_id=cid, stream=stream, ready=online,  # type: ignore[arg-type]
                                       source_online=online, readers=0, bytes_received=1000 if online else 0,
                                       tracks=["H264"] if online else [],
                                       recording=src.record and stream == "main" and online)
        return out

    async def list_recordings(self, camera_id: str, start: datetime | None,
                              end: datetime | None) -> list[RecordingSpan]:
        spans = self.spans.get(camera_id)
        if spans is None:
            spans = default_spans(self.clock())
        return [s for s in spans if (start is None or s.end > start) and (end is None or s.start < end)]

    def playback_get_url(self, camera_id: str, start: datetime, duration: float,
                         fmt: Literal["fmp4", "mp4"] = "fmp4") -> str:
        return (f"http://127.0.0.1:9996/get?path={mtx_path(camera_id, 'main')}&start={start.isoformat()}"
                f"&duration={duration}&format={fmt}")

    def whep_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        return f"http://127.0.0.1:8889/{mtx_path(camera_id, stream)}/whep"

    def rtsp_read_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        return f"rtsp://127.0.0.1:8554/{mtx_path(camera_id, stream)}"

    async def disk_usage(self) -> DiskUsage:
        return DiskUsage(path="/fake", total=1_000_000_000_000, used=400_000_000_000,
                         free=600_000_000_000, percent=40.0)


class FakeDeviceClient:
    def __init__(self, vendor: Vendor = "hikvision", *, channels: int = 4, password_ok: bool = True,
                 reachable: bool = True, kind: Literal["camera", "nvr"] = "nvr") -> None:
        self.vendor = vendor
        self.channels = channels
        self.password_ok = password_ok
        self.reachable = reachable
        self.kind = kind
        self.closed = False

    def _check(self) -> None:
        if not self.reachable:
            raise DeviceUnreachable("El equipo no responde (tiempo agotado)")
        if not self.password_ok:
            raise DeviceAuthFailed("Usuario o contraseña incorrectos")

    async def probe(self) -> DeviceInfo:
        self._check()
        return DeviceInfo(vendor=self.vendor, kind=self.kind, model="FAKE-NVR-8", serial="FAKE123",
                          firmware="V1.0", channel_count=self.channels)

    async def list_channels(self) -> list[ChannelInfo]:
        self._check()
        return [ChannelInfo(channel=i, name=f"Cámara {i}", online=i != 3, main_codec="H.264", sub_codec="H.264")
                for i in range(1, self.channels + 1)]

    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        self._check()
        return SNAPSHOT

    async def aclose(self) -> None:
        self.closed = True
