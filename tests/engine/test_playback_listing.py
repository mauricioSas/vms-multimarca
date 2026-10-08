"""Línea de tiempo del motor: «sin grabaciones» solo cuando lo es (sin procesos externos).

- Una cámara que nunca grabó no se pregunta a MediaMTX: dejaba un «ERR [playback] GetFileAttributesEx» en engine.log y
  vms.log cada 10 min por cámara (la vigilancia de operación pide el resumen del día).
- Sin carpeta de grabaciones general es un error (disco desconectado), nunca «nunca ha grabado».
- Una cámara desactivada (fuera del motor a propósito) no da error.
- Un día sin segmentos (cámara apagada todo el día, antes de la primera grabación, carpeta vacía) tampoco se pregunta
  tal cual: MediaMTX contestaría 404 y lo registraría como ERR en cada consulta.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.engine.recording_doubles import CAM, SRC, api_of, cam_dir, make_engine, now, segment
from vms.core.errors import EngineError, EngineUnavailable
from vms.core.interfaces import RecordingSpan
from vms.core.paths import AppPaths
from vms.core.recordings import PLAYBACK_DENIED, PLAYBACK_ENGINE_DOWN, PLAYBACK_NOT_CONFIGURED, RECORDINGS_DIR_MISSING
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine


@pytest.fixture
def eng(settings: VmsSettings, app_paths: AppPaths) -> MediaMtxEngine:
    return make_engine(settings, app_paths)


async def test_camera_that_never_recorded_is_not_asked_to_mediamtx(eng: MediaMtxEngine) -> None:
    assert await eng.list_recordings(CAM, None, None) == []
    assert api_of(eng).list_calls == []


async def test_camera_with_recordings_is_delegated(eng: MediaMtxEngine) -> None:
    segment(eng, now() - timedelta(minutes=5))
    api_of(eng).spans = [RecordingSpan(start=now(), duration=60.0)]
    assert len(await eng.list_recordings(CAM, None, None)) == 1
    assert api_of(eng).list_calls[0][0] == f"{CAM}/main"


# --------------------------------------------------------------- días sin segmentos (MediaMTX: 404 + ERR en su registro)
def day(offset: int = 0) -> tuple[datetime, datetime]:
    """[00:00, 24:00) UTC de hoy + `offset` días."""
    start = now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
    return start, start + timedelta(days=1)


async def test_empty_folder_is_not_asked(eng: MediaMtxEngine) -> None:
    cam_dir(eng).mkdir(parents=True)
    (cam_dir(eng) / "notas.txt").write_text("x")
    assert await eng.list_recordings(CAM, *day()) == []
    assert await eng.list_recordings(CAM, None, None) == []
    assert api_of(eng).list_calls == []
    eng._api_ok = False   # en el motor y el motor caído: se dice eso, no «sin grabaciones»
    with pytest.raises(EngineUnavailable):
        await eng.list_recordings(CAM, None, None)


async def test_day_before_the_first_segment_is_not_asked(eng: MediaMtxEngine) -> None:
    first, _ = day(-1)
    segment(eng, first + timedelta(hours=10))
    assert await eng.list_recordings(CAM, *day(-2)) == []
    assert api_of(eng).list_calls == []


async def test_day_with_a_segment_that_starts_in_it_is_asked_as_is(eng: MediaMtxEngine) -> None:
    start, end = day(-1)
    segment(eng, start + timedelta(hours=10))
    api_of(eng).spans = [RecordingSpan(start=start + timedelta(hours=10), duration=900.0)]
    assert len(await eng.list_recordings(CAM, start, end)) == 1
    assert api_of(eng).list_calls == [(f"{CAM}/main", start, end)]


async def test_dates_without_time_zone_are_utc(eng: MediaMtxEngine) -> None:
    start, end = day(-1)
    segment(eng, start + timedelta(hours=10))
    api_of(eng).spans = [RecordingSpan(start=start + timedelta(hours=10), duration=900.0)]
    assert len(await eng.list_recordings(CAM, start.replace(tzinfo=None), end.replace(tzinfo=None))) == 1


async def test_camera_off_all_day_only_asks_for_its_last_segment(eng: MediaMtxEngine) -> None:
    """La cámara grabó ayer y hoy no: el resumen de hoy (cada 10 min) y Reproducción (cada 30 s) dejaban un
    «ERR [playback] no recording segments found» en engine.log en cada consulta."""
    start, end = day()
    last = start - timedelta(hours=14)
    segment(eng, start - timedelta(days=1))
    segment(eng, last)
    api_of(eng).spans = [RecordingSpan(start=last, duration=900.0)]   # acabó ayer
    assert await eng.list_recordings(CAM, start, end) == []
    assert api_of(eng).list_calls == [(f"{CAM}/main", last - timedelta(seconds=1), start + timedelta(seconds=1))]


async def test_last_segment_that_reaches_into_the_day_is_listed(eng: MediaMtxEngine) -> None:
    start, end = day()
    last = start - timedelta(minutes=5)   # empezó a las 23:55 y sigue (o duraba más que el ajuste actual)
    segment(eng, last)
    api_of(eng).spans = [RecordingSpan(start=last, duration=900.0)]
    assert len(await eng.list_recordings(CAM, start, end)) == 1
    assert [c[1:] for c in api_of(eng).list_calls] == [(last - timedelta(seconds=1), start + timedelta(seconds=1)),
                                                       (start, end)]


async def test_engine_down_is_said_even_if_the_camera_never_recorded(eng: MediaMtxEngine) -> None:
    eng._api_ok = False
    with pytest.raises(EngineUnavailable) as info:
        await eng.list_recordings(CAM, None, None)
    assert info.value.message == PLAYBACK_ENGINE_DOWN and "Qué hacer:" in info.value.message


async def test_missing_recordings_root_is_an_error_not_never_recorded(eng: MediaMtxEngine) -> None:
    """Disco externo desconectado: la carpeta de la cámara tampoco existe, pero NO es «nunca ha grabado»."""
    Path(eng.recordings_dir).rmdir()
    with pytest.raises(EngineError) as info:
        await eng.list_recordings(CAM, None, None)
    assert (info.value.code, info.value.status, info.value.message) == ("engine_error", 502, RECORDINGS_DIR_MISSING)
    assert api_of(eng).list_calls == []


async def test_unreadable_folder_is_asked_so_the_real_error_shows(eng: MediaMtxEngine,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    real_is_dir = Path.is_dir

    def is_dir(self: Path) -> bool:
        if self.name == "main":
            raise PermissionError(13, "Access is denied")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    api_of(eng).list_error = EngineError(PLAYBACK_DENIED, details={"kind": "denied"})
    with pytest.raises(EngineError) as info:
        await eng.list_recordings(CAM, None, None)
    assert info.value.message == PLAYBACK_DENIED and len(api_of(eng).list_calls) == 1


async def test_disabled_camera_without_recordings_is_empty_even_with_the_engine_down(eng: MediaMtxEngine) -> None:
    eng._sources = {}
    eng._api_ok = False
    assert await eng.list_recordings(CAM, None, None) == []
    assert api_of(eng).list_calls == []


async def test_disabled_camera_with_recordings_lists_them_and_tolerates_sync_lag(eng: MediaMtxEngine) -> None:
    eng._sources = {}
    segment(eng, now() - timedelta(days=2))
    api_of(eng).spans = [RecordingSpan(start=now() - timedelta(days=2), duration=900.0)]
    assert len(await eng.list_recordings(CAM, None, None)) == 1   # si MediaMTX la cubre, se listan
    api_of(eng).list_error = EngineError(PLAYBACK_NOT_CONFIGURED, details={"kind": "not_configured"})
    assert await eng.list_recordings(CAM, None, None) == []        # fuera del motor a propósito: no es un error
    eng._sources = {CAM: SRC}
    with pytest.raises(EngineError):                               # en el motor: sí es un retraso a explicar
        await eng.list_recordings(CAM, None, None)
