"""Reproducción y retención contra MediaMTX REAL (guarda de contrato para futuras versiones de MediaMTX).

- Lo que el producto da por «sin grabaciones» (carpeta inexistente → 400, carpeta vacía → 404) sigue siéndolo; una
  ruta no configurada o una carpeta sin permiso de lectura son errores de verdad.
- Una ruta inerte (`retention_paths`) hace que el limpiador de MediaMTX borre los segmentos caducados de una cámara
  que ya no está en la configuración y que /list la cubra. Comprobado en Windows con v1.21.1: con una ruta regexp
  comodín el limpiador no borra nada, por eso son rutas con nombre fijo.
- Consultar una cámara que nunca grabó, o un día sin segmentos de una que sí grabó, no deja ningún «ERR [playback]»
  en el registro del motor.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from vms.core.errors import EngineError
from vms.core.interfaces import CameraSource
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.mtx_auth import MtxCredentials
from vms.core.paths import AppPaths
from vms.core.recordings import PLAYBACK_DENIED, PLAYBACK_NOT_CONFIGURED
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine
from vms.engine.mtx_api import MediaMtxApi
from vms.engine.mtx_config import attach_config, render_attach, retention_paths

pytestmark = pytest.mark.needs_mediamtx


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _local_only(settings: VmsSettings) -> VmsSettings:
    """Sin WebRTC (las métricas ya van siempre apagadas): solo API, reproducción y RTSP, todos en 127.0.0.1 y
    puertos libres."""
    return settings.model_copy(update={"mtx_webrtc_address": "", "mtx_webrtc_ice_udp": "",
                                       "mtx_webrtc_ice_tcp": ""})


def _wait_port(address: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    host, _, port = address.rpartition(":")
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex((host, int(port))) == 0:
                return
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    raise AssertionError(f"MediaMTX no arrancó: {log.read_text(encoding='utf-8', errors='replace')[-2000:]}")


def _stamp(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d_%H-%M-%S-%f%z") + ".mp4"


def _stop(procs: list[subprocess.Popen[bytes]]) -> None:
    """Para los procesos que lanzó la prueba (nunca deja un MediaMTX huérfano escuchando)."""
    for p in procs:
        if p.poll() is None:
            p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


async def test_playback_answers_and_retention_of_cameras_out_of_the_config(settings: VmsSettings, tmp_path: Path,
                                                                           mediamtx_bin: str) -> None:
    s = _local_only(settings)
    rec = tmp_path / "rec"
    (rec / "cam-vacia001" / "main").mkdir(parents=True)           # cámara activa con carpeta vacía
    gone = rec / "cam-borrada1" / "main"                            # cámara borrada con grabaciones
    gone.mkdir(parents=True)
    expired = gone / "2020-01-01_10-00-00-000000+0100.mp4"
    expired.write_bytes(b"x")
    recent = gone / _stamp(datetime.now() - timedelta(days=1))
    recent.write_bytes(b"x")
    (rec / "cam-pausada1" / "main").mkdir(parents=True)             # en pausa: carpeta vacía, ruta inerte
    active = {f"{cam}/main": {"source": "publisher", "record": True} for cam in ("cam-nunca001", "cam-vacia001")}
    paths = {**retention_paths(rec, active), **active}
    assert set(paths) == {"cam-nunca001/main", "cam-vacia001/main", "cam-borrada1/main", "cam-pausada1/main"}
    creds = MtxCredentials.from_internal_token("token-x")
    doc = attach_config(s, RecordingSettings(), RetentionSettings(days=2), rec, paths, creds=creds)
    yml = tmp_path / "mediamtx.yml"
    yml.write_text(render_attach(doc), encoding="utf-8")
    denied = rec / "cam-denegad1" / "main"
    log = tmp_path / "mtx.log"
    with log.open("wb") as lf:
        proc = subprocess.Popen([mediamtx_bin, str(yml)], stdout=lf, stderr=subprocess.STDOUT, cwd=tmp_path)
    api = MediaMtxApi(f"http://{s.mtx_api_address}", f"http://{s.mtx_playback_address}", auth=creds.api_auth)
    try:
        _wait_port(s.mtx_playback_address, proc, log)
        # Retención: el limpiador pasa al arrancar y borra el segmento caducado de la cámara borrada
        for _ in range(50):
            if not expired.exists():
                break
            await asyncio.sleep(0.1)
        assert not expired.exists(), "MediaMTX no borró el segmento caducado de una cámara fuera de la configuración"
        assert recent.exists()
        # «Sin grabaciones» de verdad: carpeta inexistente (400) y carpeta vacía (404)
        assert await api.list_recordings("cam-nunca001/main", None, None) == []
        assert await api.list_recordings("cam-vacia001/main", None, None) == []
        # La ruta inerte cubre /list (sin ella: «not configured»)
        assert await api.list_recordings("cam-pausada1/main", None, None) == []
        with pytest.raises(EngineError) as info:
            await api.list_recordings("cam-otra0001/main", None, None)
        assert info.value.message == PLAYBACK_NOT_CONFIGURED and info.value.details["kind"] == "not_configured"
        # Con carpeta y grabaciones pero sin ruta: también «no configurada», nunca «sin grabaciones»
        orphan = rec / "cam-sinruta1" / "main"
        orphan.mkdir(parents=True)
        (orphan / _stamp(datetime.now() - timedelta(hours=1))).write_bytes(b"x")
        with pytest.raises(EngineError) as info:
            await api.list_recordings("cam-sinruta1/main", None, None)
        assert info.value.details["kind"] == "not_configured"
        if sys.platform == "win32":
            # Carpeta que el motor no puede leer: antes «sin grabaciones», ahora el error con qué hacer
            paths["cam-denegad1/main"] = {"source": "publisher", "record": False}
            denied.mkdir(parents=True)
            yml.write_text(render_attach(attach_config(s, RecordingSettings(), RetentionSettings(days=2), rec, paths,
                                                       creds=creds)), encoding="utf-8")
            # Solo «listar carpeta» (RD): con RX también se deniega leer la ACL y ya no se puede deshacer sin admin
            subprocess.run(["icacls", str(denied), "/deny", "*S-1-1-0:(RD)"], check=True, capture_output=True)
            try:
                for _ in range(50):   # MediaMTX recarga el YAML al cambiar
                    try:
                        await api.list_recordings("cam-denegad1/main", None, None)
                    except EngineError as exc:
                        if exc.details.get("kind") == "denied":
                            break
                    await asyncio.sleep(0.1)
                with pytest.raises(EngineError) as info:
                    await api.list_recordings("cam-denegad1/main", None, None)
                assert info.value.message == PLAYBACK_DENIED
            finally:
                subprocess.run(["icacls", str(denied), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)
    finally:
        await api.aclose()
        _stop([proc])


async def test_camera_that_never_recorded_leaves_no_playback_error_in_the_engine_log(
        settings: VmsSettings, app_paths: AppPaths, mediamtx_bin: str, caplog: pytest.LogCaptureFixture) -> None:
    """Reproduce el ruido del PC del usuario: «ERR [playback] GetFileAttributesEx …\\cam-…\\main: The system cannot
    find the path specified.» cada 10 min por cámara sin vídeo, en engine.log y en vms.log."""
    eng = MediaMtxEngine(_local_only(settings), app_paths, mediamtx_bin=Path(mediamtx_bin))
    caplog.set_level(logging.DEBUG, logger="vms.engine.mediamtx")
    await eng.start()
    try:
        cam = "cam-0000beef"
        await eng.apply([CameraSource(camera_id=cam, name="Sin vídeo", record=True, sub_url=None,
                                      main_url=f"rtsp://127.0.0.1:{_closed_port()}/sin-video")],
                        RecordingSettings(), RetentionSettings(), eng.recordings_dir)
        assert not (Path(eng.recordings_dir) / cam).exists()
        for _ in range(3):
            assert await eng.list_recordings(cam, None, None) == []
        await asyncio.sleep(1.0)   # la salida de MediaMTX llega por tubería
    finally:
        await eng.stop()
    playback = [r.getMessage() for r in caplog.records
                if r.name == "vms.engine.mediamtx" and "[playback]" in r.getMessage() and r.levelno >= logging.WARNING]
    assert playback == []


@pytest.mark.needs_ffmpeg
async def test_day_without_segments_leaves_no_playback_error_and_the_rest_is_listed(
        settings: VmsSettings, app_paths: AppPaths, tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str,
        caplog: pytest.LogCaptureFixture) -> None:
    """Una cámara que grabó ayer y hoy no (apagada todo el día): MediaMTX contestaba 404 y dejaba «ERR [playback] no
    recording segments found» en engine.log en cada consulta (resumen de hoy cada 10 min, Reproducción cada 30 s).
    Ahora no se le pregunta así, y lo que sí hay se sigue listando, también dentro de un segmento que empieza antes
    del rango pedido."""
    caplog.set_level(logging.DEBUG, logger="vms.engine.mediamtx")
    url, procs = _fake_camera(tmp_path, mediamtx_bin, ffmpeg_bin)
    eng: MediaMtxEngine | None = None
    try:
        eng = MediaMtxEngine(_local_only(settings), app_paths, mediamtx_bin=Path(mediamtx_bin),
                             recording_check_interval=3600)
        await eng.start()
        rec = Path(eng.recordings_dir)
        live = "cam-graba002"
        sources = [CameraSource(camera_id=live, name="Puerta", main_url=url, sub_url=None, record=True)]
        recording = RecordingSettings(segment_seconds=60, part_seconds=1)
        await eng.apply(sources, recording, RetentionSettings(), eng.recordings_dir)
        folder = rec / live / "main"
        for _ in range(150):
            if folder.is_dir() and any(folder.glob("*.mp4")):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(4.0)
        _stop(procs)          # se apaga la cámara: MediaMTX cierra el segmento
        await asyncio.sleep(1.5)
        segment = sorted(folder.glob("*.mp4"))[-1]
        # Otra cámara, ya borrada, con ese segmento ayer a las 10:00: solo la cubre su ruta inerte
        old = "cam-ayer0001"
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        at_10 = today - timedelta(days=1) + timedelta(hours=10)
        (rec / old / "main").mkdir(parents=True)
        shutil.copyfile(segment, rec / old / "main" / _stamp(at_10))
        await eng.apply(sources, recording, RetentionSettings(), eng.recordings_dir)
        caplog.clear()
        assert await eng.list_recordings(old, today, today + timedelta(days=1)) == []        # hoy, apagada
        assert await eng.list_recordings(old, today - timedelta(days=3), today - timedelta(days=2)) == []  # antes
        spans = await eng.list_recordings(old, today - timedelta(days=1), today)
        assert len(spans) == 1 and abs((spans[0].start - at_10).total_seconds()) < 1
        inside = await eng.list_recordings(old, at_10 + timedelta(seconds=1), at_10 + timedelta(seconds=2))
        assert inside, "dentro de un segmento que empieza antes del rango también hay grabación"
        assert await eng.list_recordings(live, today, today + timedelta(days=1))
        await asyncio.sleep(1.0)   # la salida de MediaMTX llega por tubería
    finally:
        try:
            if eng is not None:
                await eng.stop()
        finally:
            _stop(procs)
    playback = [r.getMessage() for r in caplog.records
                if r.name == "vms.engine.mediamtx" and "[playback]" in r.getMessage()]
    assert playback == []


# --------------------------------------------------------------------------------------------- «Grabando» de verdad
def _fake_camera(tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str) -> tuple[str, list[subprocess.Popen[bytes]]]:
    """Una «cámara» local: otro MediaMTX en 127.0.0.1 al que ffmpeg publica un patrón de prueba."""
    work = tmp_path / "camara"
    work.mkdir()
    rtsp = _closed_port()
    (work / "cam.yml").write_text(
        "logLevel: warn\napi: no\nplayback: no\nmetrics: no\npprof: no\nrtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
        f"moq: no\nrtspAddress: 127.0.0.1:{rtsp}\nrtspTransports: [tcp]\n"
        "authInternalUsers:\n  - user: any\n    pass:\n    ips: ['127.0.0.1']\n    permissions:\n"
        "      - action: publish\n      - action: read\npaths:\n  cam:\n    source: publisher\n", encoding="utf-8")
    log = work / "cam.log"
    with log.open("wb") as lf:
        mtx = subprocess.Popen([mediamtx_bin, str(work / "cam.yml")], stdout=lf, stderr=subprocess.STDOUT, cwd=work)
    procs = [mtx]
    try:
        _wait_port(f"127.0.0.1:{rtsp}", mtx, log)
        procs.insert(0, subprocess.Popen(
            [ffmpeg_bin, "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "120",
             "-c:v", "libx264", "-g", "10", "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:{rtsp}/cam"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    except BaseException:
        _stop(procs)   # si no arranca, que no quede un MediaMTX huérfano escuchando
        raise
    return f"rtsp://127.0.0.1:{rtsp}/cam", procs


@pytest.mark.needs_ffmpeg
async def test_recording_state_comes_from_segments_and_recorder_errors(
        settings: VmsSettings, app_paths: AppPaths, tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str) -> None:
    """Con MediaMTX real: «Grabando» solo cuando /list ve el segmento crecer; en Windows, una carpeta de grabaciones
    sin permiso de escritura da «No graba» con el motivo (antes: «Grabando» con solo tener vídeo)."""
    url, procs = _fake_camera(tmp_path, mediamtx_bin, ffmpeg_bin)
    eng: MediaMtxEngine | None = None
    rec = app_paths.recordings_dir
    denied = False
    try:
        eng = MediaMtxEngine(_local_only(settings), app_paths, mediamtx_bin=Path(mediamtx_bin),
                             recording_check_interval=3600)
        rec = Path(eng.recordings_dir)
        await eng.start()
        if sys.platform == "win32":
            # Nadie puede escribir en la carpeta de grabaciones: MediaMTX no podrá crear la de la cámara
            subprocess.run(["icacls", str(rec), "/deny", "*S-1-1-0:(OI)(CI)(W,D)"], check=True, capture_output=True)
            denied = True
        cam = "cam-graba001"
        await eng.apply([CameraSource(camera_id=cam, name="Puerta", main_url=url, sub_url=None, record=True)],
                        RecordingSettings(segment_seconds=60, part_seconds=1), RetentionSettings(), eng.recordings_dir)
        for _ in range(150):
            st = (await eng.paths_status())[f"{cam}/main"]
            if st.ready and (not denied or st.recording_error):
                break
            await asyncio.sleep(0.1)
        if denied:
            assert st.ready and not st.recording, "con vídeo pero sin poder escribir NO es «Grabando»"
            assert "permiso" in st.recording_error and "Qué hacer:" in st.recording_error
            subprocess.run(["icacls", str(rec), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)
            denied = False
        for _ in range(150):   # la grabación empieza (o se reanuda tras el error) en el siguiente fotograma clave
            if (rec / cam / "main").is_dir() and any((rec / cam / "main").glob("*.mp4")):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(2.5)
        await eng.check_recording()
        st = (await eng.paths_status())[f"{cam}/main"]
        assert st.recording and st.recording_error == "" and st.last_recorded_at is not None
        assert (datetime.now().astimezone() - st.last_recorded_at).total_seconds() < 10
        assert await eng.list_recordings(cam, None, None)
    finally:
        if denied:
            subprocess.run(["icacls", str(rec), "/remove:d", "*S-1-1-0"], capture_output=True)
        try:
            if eng is not None:
                await eng.stop()
        finally:
            _stop(procs)
