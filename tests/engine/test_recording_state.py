"""«Grabando» de verdad (motor, sin procesos externos).

Antes, una ruta estaba «Grabando» con solo tener vídeo y `record`, aunque MediaMTX no pudiera escribir nada (disco
lleno, desconectado o sin permiso). Ahora hace falta que lleguen segmentos nuevos, y los errores «[recorder]» de
MediaMTX llegan a la interfaz con «Qué hacer:».
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.engine.recording_doubles import CAM, SRC, api_of, make_engine, mtx_time, now, segment
from vms.core.errors import EngineError, EngineUnavailable
from vms.core.interfaces import RecordingSpan
from vms.core.paths import AppPaths
from vms.core.recordings import PLAYBACK_DISABLED, PLAYBACK_FAILED
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine
from vms.engine.engine import (RECORDER_ERROR_TTL_S, RECORDING_GRACE_S, RECORDING_STALE_MSG, explain_recorder_error,
                               newest_segment_start)


@pytest.fixture
def eng(settings: VmsSettings, app_paths: AppPaths) -> MediaMtxEngine:
    return make_engine(settings, app_paths)


def ready_item(ready_since: datetime) -> dict[str, Any]:
    return {"name": f"{CAM}/main", "ready": True, "readyTime": mtx_time(ready_since), "tracks": ["H264"],
            "readers": [], "inboundBytes": 1000}


async def main_status(e: MediaMtxEngine) -> Any:
    return (await e.paths_status())[f"{CAM}/main"]


async def test_recorder_error_in_engine_log_means_not_recording(eng: MediaMtxEngine) -> None:
    api_of(eng).items = [ready_item(now() - timedelta(hours=1))]
    assert (await main_status(eng)).recording is True
    eng._on_line(f"2026/10/08 14:51:04 ERR [path {CAM}/main] [recorder] open C:\\ProgramData\\VMSMultimarca\\"
                 f"recordings\\{CAM}\\main\\2026-10-08_14-51-03-226320+0200.mp4: Access is denied.", relay=False)
    st = await main_status(eng)
    assert st.ready and not st.recording
    assert "permiso para escribir" in st.recording_error and "Qué hacer:" in st.recording_error
    assert st.last_error == st.recording_error


@pytest.mark.parametrize("raw,expected", [
    ("mkdir D:\\VMS: The system cannot find the path specified.", "disco externo"),
    ("write D:\\VMS\\x.mp4: There is not enough space on the disk.", "lleno"),
    ("open /rec/x.mp4: permission denied", "permiso"),
    ("algo raro", "reinicia el PC"),
])
def test_recorder_errors_are_explained_with_what_to_do(raw: str, expected: str) -> None:
    text = explain_recorder_error(raw)
    assert expected in text and "Qué hacer:" in text and raw[:20] in text


def test_recorder_error_detail_hides_credentials() -> None:
    assert "S3cr3t" not in explain_recorder_error("open rtsp://admin:S3cr3t@10.0.0.5/x: algo")


async def test_fresh_segments_mean_recording_and_stale_ones_do_not(eng: MediaMtxEngine) -> None:
    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(hours=1))]
    newest = now() - timedelta(seconds=300)
    segment(eng, newest)
    api.spans = [RecordingSpan(start=newest, duration=295.0)]          # termina hace 5 s
    await eng.check_recording()
    path, start, _ = api.list_calls[-1]
    assert path == f"{CAM}/main" and start is not None and start <= newest   # solo el último segmento
    st = await main_status(eng)
    assert st.recording and st.recording_error == "" and st.last_recorded_at is not None
    api.spans = [RecordingSpan(start=newest, duration=100.0)]          # dejó de crecer hace 200 s
    await eng.check_recording()
    st = await main_status(eng)
    assert not st.recording and st.recording_error == RECORDING_STALE_MSG and "Qué hacer:" in st.last_error


async def test_old_last_segment_is_not_recording_without_asking_mediamtx(eng: MediaMtxEngine) -> None:
    """Sin segmento reciente ni se pregunta: /list contestaría 404 y MediaMTX lo registra como ERR."""
    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(hours=3))]
    segment(eng, now() - timedelta(hours=2))
    await eng.check_recording()
    assert api.list_calls == []
    assert not (await main_status(eng)).recording


async def test_just_connected_camera_gets_a_grace_period(eng: MediaMtxEngine) -> None:
    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(seconds=20))]   # el primer segmento espera al fotograma clave
    await eng.check_recording()
    assert (await main_status(eng)).recording
    api.items = [ready_item(now() - timedelta(minutes=10))]   # se reconectó: otro readyTime, aún sin comprobar
    assert (await main_status(eng)).recording
    await eng.check_recording()
    st = await main_status(eng)
    assert not st.recording and st.recording_error == RECORDING_STALE_MSG


async def test_recorder_error_clears_when_a_new_segment_arrives(eng: MediaMtxEngine) -> None:
    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(hours=1))]
    eng._on_line(f"2026/10/08 14:51:04 ERR [path {CAM}/main] [recorder] mkdir D:\\VMS: The system cannot find the "
                 "path specified.", relay=False)
    assert not (await main_status(eng)).recording
    newest = now() - timedelta(seconds=10)
    segment(eng, newest)
    api.spans = [RecordingSpan(start=newest, duration=11.0)]     # el disco ha vuelto: termina después del error
    await eng.check_recording()
    st = await main_status(eng)
    assert st.recording and st.recording_error == ""


async def test_unreadable_recordings_folder_is_unknown_not_not_recording(eng: MediaMtxEngine,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión final: si el backend no puede listar la carpeta de grabación (ACL de una carpeta a medida), se
    tomaba como «ningún segmento reciente» y a los 90 s salía «No graba» aunque MediaMTX estuviera escribiendo;
    también bajaba `cameras_recording` de la salud del actualizador. Sin poder leerla no se sabe: vale la palabra
    de MediaMTX y sus errores «[recorder]»."""
    from vms.engine import engine as engine_mod

    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(hours=1))]
    segment(eng, now() - timedelta(seconds=30))              # la carpeta existe…
    monkeypatch.setattr(engine_mod, "segment_starts", lambda folder: None)   # …pero no se puede listar
    await eng.check_recording()
    st = await main_status(eng)
    assert st.recording and st.recording_error == "" and api.list_calls == []
    # un error «[recorder]» sigue mandando
    eng._on_line(f"2026/10/08 14:51:04 ERR [path {CAM}/main] [recorder] open C:\\x\\{CAM}\\main\\a.mp4: Access is "
                 "denied.", relay=False)
    assert not (await main_status(eng)).recording


async def test_camera_without_video_is_not_checked(eng: MediaMtxEngine) -> None:
    api = api_of(eng)
    api.items = [{"name": f"{CAM}/main", "ready": False, "readyTime": None}]
    segment(eng, now() - timedelta(seconds=30))
    await eng.check_recording()
    assert api.list_calls == [] and not (await main_status(eng)).recording


async def test_recorder_error_repeated_with_another_file_is_logged_once(eng: MediaMtxEngine,
                                                                        caplog: pytest.LogCaptureFixture) -> None:
    """MediaMTX reintenta cada ~4 s con otro segmento: la clave de «ya avisado» no puede llevar el nombre del archivo
    (antes, una línea ERROR por cámara cada pocos segundos en vms.log)."""
    caplog.set_level(logging.DEBUG, logger="vms.engine.mediamtx")
    for stamp in ("14-51-03-226320", "14-51-07-311002", "14-51-11-402117"):
        eng._on_line(f"2026/10/08 14:51:04 ERR [path {CAM}/main] [recorder] open C:\\ProgramData\\VMSMultimarca\\"
                     f"recordings\\{CAM}\\main\\2026-10-08_{stamp}+0200.mp4: Access is denied.", relay=False)
    errors = [r for r in caplog.records if r.name == "vms.engine.mediamtx" and r.levelno >= logging.WARNING]
    assert len(errors) == 1


async def test_recorder_error_from_before_a_reconnection_does_not_count(eng: MediaMtxEngine) -> None:
    """La cámara se cortó después del error y volvió grabando bien: no puede seguir en «No graba» hasta la siguiente
    comprobación (antes, hasta 30 s en rojo)."""
    api_of(eng).items = [ready_item(now() - timedelta(hours=1))]
    eng._on_line(f"2026/10/08 14:51:04 ERR [path {CAM}/main] [recorder] mkdir D:\\VMS: The system cannot find the "
                 "path specified.", relay=False)
    assert not (await main_status(eng)).recording
    api_of(eng).items = [ready_item(now() + timedelta(seconds=1))]   # reconectada después del error
    st = await main_status(eng)
    assert st.recording and st.recording_error == ""


async def test_recorder_error_that_stops_repeating_expires_without_playback(eng: MediaMtxEngine) -> None:
    """Sin servidor de reproducción la comprobación no puede ver tramos nuevos: antes se cortaba en la primera cámara
    (EngineUnavailable) y el error del grabador se quedaba para siempre aunque se hubiera arreglado."""
    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(hours=1))]
    segment(eng, now() - timedelta(seconds=60))
    api.list_error = EngineUnavailable(PLAYBACK_DISABLED)
    text = explain_recorder_error("mkdir D:\\VMS: The system cannot find the path specified.")
    eng._recorder_errors[f"{CAM}/main"] = (now() - timedelta(seconds=10), text)   # aún se repite
    await eng.check_recording()
    assert (await main_status(eng)).recording_error == text
    eng._recorder_errors[f"{CAM}/main"] = (now() - timedelta(seconds=RECORDER_ERROR_TTL_S + 5), text)
    await eng.check_recording()
    st = await main_status(eng)
    assert st.recording and st.recording_error == "" and f"{CAM}/main" not in eng._recorder_errors


async def test_turning_recording_on_gets_the_grace_period(eng: MediaMtxEngine) -> None:
    """Al marcar «Grabar 24/7» MediaMTX no reinicia la ruta (mismo readyTime) y el primer segmento espera al
    fotograma clave: no es «No graba» todavía."""
    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(hours=1))]
    eng._sources = {CAM: SRC.model_copy(update={"record": False})}
    await eng.check_recording()
    eng._sources = {CAM: SRC}
    await eng.check_recording()
    assert (await main_status(eng)).recording
    eng._rec_checks[f"{CAM}/main"].since = now() - timedelta(seconds=RECORDING_GRACE_S + 1)
    await eng.check_recording()
    st = await main_status(eng)
    assert not st.recording and st.recording_error == RECORDING_STALE_MSG


async def test_one_list_error_is_not_enough_for_not_recording(eng: MediaMtxEngine) -> None:
    api = api_of(eng)
    api.items = [ready_item(now() - timedelta(hours=1))]
    segment(eng, now() - timedelta(seconds=60))
    api.list_error = EngineError(PLAYBACK_FAILED, details={"kind": "other"})
    await eng.check_recording()
    assert (await main_status(eng)).recording
    await eng.check_recording()
    st = await main_status(eng)
    assert not st.recording and st.recording_error == PLAYBACK_FAILED


def test_newest_segment_by_name(tmp_path: Path) -> None:
    d = tmp_path / "main"
    d.mkdir()
    for name in ("2026-10-08_10-00-00-000000+0200.mp4", "2026-10-08_10-15-00-000000+0200.mp4", "notas.txt"):
        (d / name).write_text("x")
    assert newest_segment_start(d) == datetime(2026, 10, 8, 8, 15, tzinfo=timezone.utc)
    assert newest_segment_start(tmp_path / "no-existe") is None
