"""«Grabando» de verdad en /api/status (CONTRATO §6.8): una cámara con vídeo que no guarda nada lo dice y cuenta como
problema. Antes `recording` era «vídeo + `record`» y el panel ponía «Grabando» aunque el disco estuviera lleno."""
from __future__ import annotations

from tests.api.conftest import Harness, new_device


async def test_status_shows_cameras_with_video_that_do_not_record(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1, 2])
    await api.settle()
    cam = str(dev["cameras"][0])
    reason = "No se puede guardar la grabación: el disco de grabación está lleno. Qué hacer: libera espacio."
    api.engine.not_saving[cam] = reason
    api.state._paths_cache = None
    op = await api.operator()
    st = (await op.get("/api/status")).json()
    row = next(c for c in st["cameras"] if c["camera_id"] == cam)
    assert row["online"] is True and row["recording"] is False
    assert row["recording_error"] == reason and row["last_error"] == reason
    assert "cámaras con vídeo que no graban" in st["problems"] and st["status"] == "degraded"
    other = next(c for c in st["cameras"] if c["camera_id"] != cam)
    assert other["recording"] is True and other["recording_error"] == "" and "last_recorded_at" in other
