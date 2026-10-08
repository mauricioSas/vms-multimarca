"""Puertos ocupados por otro programa con MediaMTX de verdad (modo `attach`, CONTRATO §4.1 y §13.10).

Reproduce el fallo del PC del usuario: HikCentral (servicio SYS) usa 127.0.0.1:9998 para su WebSocket y el
`.env` de la v1 fijaba ahí las métricas de MediaMTX. MediaMTX abre las métricas lo primero y, con el puerto
ocupado, se cerraba al arrancar con «listen tcp 127.0.0.1:9998: bind: …»; `vmsctl run` lo relanzaba sin fin y
Estado del sistema solo decía «no responde».

El «otro programa» es un socket de esta prueba en la dirección EXACTA (127.0.0.1:puerto): con el mismo usuario
de Windows, un socket en 0.0.0.0 no impide abrir 127.0.0.1 (entre cuentas distintas, como SYS y VMSEngine, sí).
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from tests.conftest import get_free_port
from vms.core.interfaces import CameraSource
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings, load_settings
from vms.engine import MediaMtxEngine
from vms.engine.mtx_config import split_address
from vms.engine.service import run_engine, write_engine_config

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx]


def hold_port(port: int) -> socket.socket:
    """Otro programa escuchando en 127.0.0.1:port (sin SO_REUSEADDR, como un servidor normal)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def env_settings(tmp_path: Path, app_paths: AppPaths, **extra: str) -> VmsSettings:
    """Ajustes leídos de un .env como el de una instalación (todas las direcciones del motor libres)."""
    values = {
        "VMS_DATA_DIR": str(app_paths.base), "VMS_INTERNAL_TOKEN": "token-interno-de-pruebas",
        "VMS_CREDENTIAL_BACKEND": "file", "VMS_ENGINE_MODE": "attach",
        "VMS_MTX_RTSP_ADDRESS": f"127.0.0.1:{get_free_port()}", "VMS_MTX_WEBRTC_ADDRESS": f"127.0.0.1:{get_free_port()}",
        "VMS_MTX_WEBRTC_ICE_UDP": f":{get_free_port()}", "VMS_MTX_WEBRTC_ICE_TCP": "off",
        "VMS_MTX_API_ADDRESS": f"127.0.0.1:{get_free_port()}",
        "VMS_MTX_PLAYBACK_ADDRESS": f"127.0.0.1:{get_free_port()}", **extra,
    }
    env = tmp_path / "instalado.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return load_settings(env_file=env)


@pytest.fixture
def engine_service(app_paths: AppPaths, mediamtx_bin: str) -> Iterator[threading.Event]:
    """El «servicio VMSEngine» (lo que hace `vmsctl run` en Windows): espera a que exista el YAML, lanza MediaMTX
    con él y lo relanza si cae, con su salida en logs/engine.log."""
    stop = threading.Event()
    t = threading.Thread(target=run_engine, args=(app_paths, Path(mediamtx_bin), stop),
                         kwargs={"backoff": (0.5, 1.0)}, daemon=True, name="vmsengine")
    t.start()
    yield stop
    stop.set()
    t.join(15)
    assert not t.is_alive(), "el servicio del motor debe parar"


async def wait_api(engine: MediaMtxEngine, timeout: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if (await engine.status()).api_ok:
            return True
        await asyncio.sleep(0.25)
    return False


def attach_backend(settings: VmsSettings, app_paths: AppPaths) -> MediaMtxEngine:
    return MediaMtxEngine(settings, app_paths, mode="attach", disk_guard_interval=3600, attach_poll=0.2,
                          logtail_interval=0.2)


async def test_engine_starts_although_the_old_metrics_port_is_taken(tmp_path: Path, app_paths: AppPaths,
                                                                    engine_service: threading.Event) -> None:
    taken = get_free_port()
    holder = hold_port(taken)
    try:
        # .env de la v1: VMS_MTX_METRICS_ADDRESS apunta a un puerto que ya usa otro programa
        settings = env_settings(tmp_path, app_paths, VMS_MTX_METRICS_ADDRESS=f"127.0.0.1:{taken}")
        write_engine_config(settings, app_paths)     # lo que hacen el instalador y el backend
        eng = attach_backend(settings, app_paths)
        try:
            await eng.start()
            ok = await wait_api(eng, 20)
            log = (app_paths.logs_dir / "engine.log").read_text(encoding="utf-8", errors="replace")
            assert ok, f"el motor no responde con el puerto {taken} ocupado:\n{log[-2000:]}"
            assert f":{taken}: bind" not in log and "[metrics]" not in log
        finally:
            await eng.stop()
    finally:
        holder.close()


async def test_status_names_the_program_holding_an_engine_port(tmp_path: Path, app_paths: AppPaths,
                                                               engine_service: threading.Event) -> None:
    """Lo que ve el usuario si otro programa ocupa un puerto que el motor sí usa: el puerto, para qué es y qué
    programa lo tiene (aquí, el propio Python de la prueba), en vez de «no responde»."""
    settings = env_settings(tmp_path, app_paths)
    hp = split_address(settings.mtx_playback_address)
    assert hp is not None
    taken = hp[1]
    holder = hold_port(taken)
    try:
        write_engine_config(settings, app_paths)
        eng = attach_backend(settings, app_paths)
        try:
            await eng.start()
            t0 = time.monotonic()
            st = await eng.status()
            while time.monotonic() - t0 < 20 and (st.problem is None or st.problem.code != "port_in_use"):
                await asyncio.sleep(0.25)
                st = await eng.status()
            assert st.problem is not None and st.problem.code == "port_in_use", st
            assert str(taken) in st.problem.detail_es and "reproducción" in st.problem.detail_es
            assert "python" in st.problem.detail_es.lower(), st.problem.detail_es
            assert not st.running and "Qué hacer:" in st.last_error
        finally:
            await eng.stop()
    finally:
        holder.close()


@pytest.mark.parametrize("role", ["api", "rtsp"])
async def test_status_names_the_taken_port_with_cameras(role: str, tmp_path: Path, app_paths: AppPaths,
                                                        engine_service: threading.Event) -> None:
    """Con cámaras, MediaMTX arranca sus rutas antes que RTSP, WebRTC y la API, y entre el «bind» y la salida
    escribe dos líneas por cámara. Con 4 o más, Estado del sistema decía «no ha dejado ningún mensaje de error».
    Las cámaras apuntan a un puerto cerrado de este equipo (sin red)."""
    settings = env_settings(tmp_path, app_paths)
    address, what = ((settings.mtx_api_address, "control del motor") if role == "api"
                     else (settings.mtx_rtsp_address, "vídeo interno"))
    hp = split_address(address)
    assert hp is not None
    taken = hp[1]
    closed = get_free_port()
    cameras = [CameraSource(camera_id=f"cam-{i:08d}", name=f"Cámara {i}", record=True,
                            main_url=f"rtsp://admin:Cl4ve@127.0.0.1:{closed}/Streaming/Channels/{i + 1}01",
                            sub_url=f"rtsp://admin:Cl4ve@127.0.0.1:{closed}/Streaming/Channels/{i + 1}02")
               for i in range(10)]
    holder = hold_port(taken)
    try:
        eng = attach_backend(settings, app_paths)
        try:
            await eng.start()
            # El backend escribe mediamtx.yml con las cámaras (modo attach) y el «servicio» lanza MediaMTX con él
            await eng.apply(cameras, RecordingSettings(), RetentionSettings(), str(app_paths.recordings_dir))
            t0 = time.monotonic()
            st = await eng.status()
            while time.monotonic() - t0 < 30 and (st.problem is None or st.problem.code != "port_in_use"):
                await asyncio.sleep(0.25)
                st = await eng.status()
            log = (app_paths.logs_dir / "engine.log").read_text(encoding="utf-8", errors="replace")
            assert st.problem is not None and st.problem.code == "port_in_use", f"{st}\n{log[-3000:]}"
            assert log.count("[RTSP source] started") >= 4, "la prueba necesita las líneas de las cámaras"
            assert str(taken) in st.problem.detail_es and what in st.problem.detail_es
            assert "python" in st.problem.detail_es.lower(), st.problem.detail_es
            assert "Cl4ve" not in st.last_error and "Qué hacer:" in st.last_error
        finally:
            await eng.stop()
    finally:
        holder.close()
