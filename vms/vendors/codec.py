"""Códecs: nombres normalizados y copia de la configuración de un flujo antes de «Corregir códec».

`StreamConfigBackup` es lo que se guarda en `config/device-backups/<equipo>/<fecha>.xml|.txt` antes de
cambiar nada en el equipo (PLAN-V2 §3.2 punto 8): con ello «Deshacer» repone exactamente lo que había.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


def normalize_codec(value: str | None) -> str | None:
    """«H.264», «H264», «h264+» → «H.264»; «HEVC», «H.265+» → «H.265»; «MJPEG»/«JPEG» → «MJPEG»."""
    if not value or not value.strip():
        return None
    v = value.strip().upper().replace(".", "").replace("+", "").replace(" ", "")
    if v.startswith("H264") or v in ("AVC", "H264H", "H264B", "H264M"):
        return "H.264"
    if v.startswith("H265") or v.startswith("HEVC"):
        return "H.265"
    if v in ("MJPEG", "JPEG", "MJPG"):
        return "MJPEG"
    return value.strip()


@dataclass(frozen=True)
class StreamConfigBackup:
    vendor: str
    channel: int
    stream: Literal["main", "sub"]
    codec: str | None                 # códec que había antes del cambio
    resource: str                     # recurso del equipo (ISAPI) o prefijo de clave (Dahua)
    raw: str                          # copia literal (XML de ISAPI o líneas «clave=valor» de Dahua)
    fmt: Literal["xml", "txt"]


@runtime_checkable
class CodecFixClient(Protocol):
    """Capacidad API_CODEC_FIX (Hikvision y Dahua)."""

    async def read_stream_config(self, channel: int, stream: Literal["main", "sub"] = "sub"
                                 ) -> StreamConfigBackup: ...

    async def set_stream_codec(self, channel: int, stream: Literal["main", "sub"], codec: str
                               ) -> StreamConfigBackup: ...

    async def restore_stream_config(self, backup: StreamConfigBackup) -> None: ...


__all__ = ["CodecFixClient", "StreamConfigBackup", "normalize_codec"]
