"""Dobles compartidos por las pruebas de reproducción, «Grabando» y retención del motor (sin procesos externos)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vms.core.interfaces import CameraSource, RecordingSpan
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine

CAM = "cam-ca3b7c25"
SRC = CameraSource(camera_id=CAM, name="Entrada", main_url="rtsp://admin:Cl4ve%231@10.0.0.5:554/Streaming/Channels/101",
                   sub_url="rtsp://admin:Cl4ve%231@10.0.0.5:554/Streaming/Channels/102", record=True)
REC = RecordingSettings(segment_seconds=900, part_seconds=1)
RET = RetentionSettings(days=30, disk_guard_percent=0)


def now() -> datetime:
    return datetime.now(timezone.utc)


def mtx_time(dt: datetime) -> str:
    """Fecha como la da MediaMTX (hora local con desfase)."""
    return dt.astimezone().isoformat()


class StubApi:
    """API de MediaMTX en memoria: apunta las consultas de /list y contesta lo que diga la prueba."""

    def __init__(self) -> None:
        self.list_calls: list[tuple[str, datetime | None, datetime | None]] = []
        self.spans: list[RecordingSpan] = []
        self.list_error: Exception | None = None
        self.items: list[dict[str, Any]] = []
        self.config_names: set[str] = set()
        self.added: dict[str, dict[str, Any]] = {}
        self.replaced: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    async def list_recordings(self, path: str, start: datetime | None, end: datetime | None) -> list[RecordingSpan]:
        self.list_calls.append((path, start, end))
        if self.list_error is not None:
            raise self.list_error
        return list(self.spans)

    async def paths_list(self) -> list[dict[str, Any]]:
        return list(self.items)

    async def config_path_names(self) -> set[str]:
        return set(self.config_names)

    async def add_path(self, name: str, conf: dict[str, Any]) -> None:
        self.added[name] = conf

    async def replace_path(self, name: str, conf: dict[str, Any]) -> None:
        self.replaced[name] = conf

    async def delete_path(self, name: str) -> bool:
        self.deleted.append(name)
        return True

    async def aclose(self) -> None:
        return None


def make_engine(settings: VmsSettings, app_paths: AppPaths) -> MediaMtxEngine:
    """Motor en modo attach «conectado» a una API simulada, con la carpeta de grabaciones creada."""
    e = MediaMtxEngine(settings, app_paths, mode="attach")
    e.api = StubApi()  # type: ignore[assignment]
    e._api_ok = True
    e._sources = {CAM: SRC}
    e._recording = REC
    Path(e.recordings_dir).mkdir(parents=True, exist_ok=True)
    return e


def api_of(e: MediaMtxEngine) -> StubApi:
    assert isinstance(e.api, StubApi)
    return e.api


def cam_dir(e: MediaMtxEngine, cam: str = CAM) -> Path:
    return Path(e.recordings_dir) / cam / "main"


def segment(e: MediaMtxEngine, start: datetime, cam: str = CAM) -> Path:
    """Segmento con el nombre que pone MediaMTX (contenido irrelevante: solo se mira el nombre)."""
    d = cam_dir(e, cam)
    d.mkdir(parents=True, exist_ok=True)
    f = d / (start.astimezone().strftime("%Y-%m-%d_%H-%M-%S-%f%z") + ".mp4")
    f.write_bytes(b"\0" * 10)
    return f
