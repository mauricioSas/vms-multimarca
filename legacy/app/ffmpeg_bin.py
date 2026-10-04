"""Localiza el ejecutable de ffmpeg (el que trae imageio-ffmpeg, sin depender del sistema)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

_cached: str | None = None


def find_ffmpeg() -> str | None:
    global _cached
    if _cached and os.path.exists(_cached):
        return _cached
    candidates = []
    if os.environ.get("VMS_FFMPEG"):
        candidates.append(os.environ["VMS_FFMPEG"])
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # binario no incluido o plataforma no soportada
        log.warning("imageio-ffmpeg no disponible: %s", exc)
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    for c in candidates:
        if c and os.path.isfile(c):
            _cached = c
            return c
    return None


def popen_kwargs() -> dict:
    """Opciones para que en Windows no se abra una consola por cada ffmpeg."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def ffmpeg_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True,
                             timeout=10, **popen_kwargs()).stdout
        return out.splitlines()[0] if out else "?"
    except Exception as exc:
        return f"error: {exc}"
