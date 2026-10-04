"""Modelo de datos: dispositivos (cámara o canal de NVR), disposiciones y ajustes."""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field

from . import rtsp

KIND_CAMERA = "camera"
KIND_NVR_CHANNEL = "nvr_channel"
KINDS = {KIND_CAMERA: "Cámara IP", KIND_NVR_CHANNEL: "Canal de NVR"}

GRID_SIZES = (1, 4, 9, 16)
MAX_WALLS = 4


def new_id() -> str:
    return uuid.uuid4().hex


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:max_len].strip("-")) or "camara"


@dataclass
class Device:
    id: str = field(default_factory=new_id)
    name: str = ""
    kind: str = KIND_CAMERA
    vendor: str = rtsp.VENDOR_HIKVISION
    host: str = ""
    port: int = 554
    username: str = ""
    channel: int = 1
    custom_paths: bool = False
    main_path: str = ""
    sub_path: str = ""
    enabled: bool = True
    record: bool = True
    record_audio: bool = False
    rec_folder: str = ""

    # --- rutas -----------------------------------------------------------
    def stream_paths(self) -> tuple[str, str]:
        """(ruta principal, ruta subflujo) efectivas según preset o ruta personalizada."""
        if not self.custom_paths and self.vendor != rtsp.VENDOR_GENERIC:
            preset = rtsp.preset_paths(self.vendor, self.channel)
            if preset:
                return preset
        main = rtsp.normalize_path(self.main_path)
        sub = rtsp.normalize_path(self.sub_path) or main
        return main, sub

    def url(self, stream: str, password: str = "") -> str:
        main, sub = self.stream_paths()
        path = main if stream == rtsp.STREAM_MAIN else sub
        return rtsp.build_rtsp_url(self.host, self.port, path, self.username, password)

    def display(self) -> str:
        return f"{self.name} · {rtsp.VENDORS.get(self.vendor, self.vendor)} · {self.host}"

    def ensure_rec_folder(self) -> None:
        if not self.rec_folder:
            self.rec_folder = f"{slugify(self.name)}__{self.id[:8]}"

    # --- serialización ---------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Device":
        if not isinstance(d, dict):
            raise ValueError("Dispositivo con formato incorrecto")
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in d.items() if k in known}
        dev = cls(**clean)
        dev.port = int(dev.port)
        dev.channel = int(dev.channel)
        dev.enabled = bool(dev.enabled)
        dev.record = bool(dev.record)
        dev.record_audio = bool(dev.record_audio)
        dev.custom_paths = bool(dev.custom_paths)
        if not dev.id:
            dev.id = new_id()
        dev.ensure_rec_folder()
        return dev


def validate_device(dev: Device, others: list[Device] | None = None) -> list[str]:
    """Devuelve la lista de errores (vacía si el dispositivo es válido)."""
    errors: list[str] = []
    if not dev.name.strip():
        errors.append("El nombre es obligatorio.")
    elif others:
        for o in others:
            if o.id != dev.id and o.name.strip().lower() == dev.name.strip().lower():
                errors.append(f"Ya existe un dispositivo llamado «{o.name}».")
                break
    host = dev.host.strip()
    if "://" in host or "/" in host:
        errors.append("En «IP o DNS» escribe solo la IP o el nombre, sin rtsp:// ni rutas.")
    elif not rtsp.is_valid_host(host):
        errors.append("La IP o el nombre DNS no es válido.")
    if not (1 <= int(dev.port) <= 65535):
        errors.append("El puerto debe estar entre 1 y 65535.")
    if dev.vendor not in rtsp.VENDORS:
        errors.append("Fabricante desconocido.")
    if dev.kind not in KINDS:
        errors.append("Tipo de dispositivo desconocido.")
    if not (1 <= int(dev.channel) <= 512):
        errors.append("El canal debe estar entre 1 y 512.")
    if dev.custom_paths or dev.vendor == rtsp.VENDOR_GENERIC:
        if not rtsp.normalize_path(dev.main_path):
            errors.append("Indica la ruta RTSP del flujo principal.")
        for p in (dev.main_path, dev.sub_path):
            if p and (" " in p.strip() or "://" in p):
                errors.append("La ruta RTSP no puede contener espacios ni rtsp://; solo la parte que va tras el puerto.")
                break
    if dev.username and ":" in dev.username:
        errors.append("El usuario no puede contener «:».")
    return errors


@dataclass
class WallLayout:
    grid: int = 4
    cells: list = field(default_factory=list)  # id de dispositivo o None por celda

    def normalized(self) -> "WallLayout":
        grid = self.grid if self.grid in GRID_SIZES else 4
        cells = list(self.cells)[:16]
        cells += [None] * (16 - len(cells))  # se guardan 16 para no perder asignaciones al cambiar de grid
        return WallLayout(grid, cells)


@dataclass
class Settings:
    recordings_dir: str = ""
    segment_seconds: int = 300
    recording_enabled: bool = True
    retention_days: int = 30          # 0 = sin límite por días
    retention_max_disk_percent: int = 90  # 0 = sin límite por disco
    display_fps: int = 15
    force_walls: int = 0              # 0 = uno por monitor detectado; 1-4 = fuerza ese número
    walls_windowed: bool = False      # muros en ventana normal (pruebas con un solo monitor)
    start_with_windows: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        if isinstance(d, dict):
            for k in cls.__dataclass_fields__:
                if k in d:
                    setattr(s, k, type(getattr(s, k))(d[k]))
        s.segment_seconds = max(30, min(3600, s.segment_seconds))
        s.display_fps = max(5, min(30, s.display_fps))
        s.retention_days = max(0, s.retention_days)
        s.retention_max_disk_percent = max(0, min(99, s.retention_max_disk_percent))
        s.force_walls = max(0, min(MAX_WALLS, s.force_walls))
        return s
