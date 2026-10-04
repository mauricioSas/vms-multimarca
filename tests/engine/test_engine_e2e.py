"""Motor real (MediaMTX supervisado) contra el simulador de cámaras Hikvision/Dahua.

Comprueba con procesos reales: grabación 24/7, /list, /get (MP4 válido según ffprobe), subflujo
bajo demanda, reconexión al matar y relanzar un flujo o apagar un equipo, relanzamiento de
MediaMTX tras un cierre inesperado, cambios en caliente, pausa por contraseña rechazada y
que ninguna contraseña acabe en disco.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import httpx
import pytest

from tools.camsim.simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice
from vms.core.credentials import CredentialStore
from vms.core.errors import EngineUnavailable
from vms.core.models import AppConfig, Camera, Device, RecordingSettings, RetentionSettings
from vms.core.mtx_auth import with_reader_credentials
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.core.sources import build_camera_sources
from vms.engine import MediaMtxEngine

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg]

REC = RecordingSettings(segment_seconds=60, part_seconds=1)
RET = RetentionSettings(days=2, disk_guard_percent=0)


async def wait_for(check: Callable[[], Awaitable[bool]], timeout: float, what: str) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await check():
            return time.monotonic() - t0
        await asyncio.sleep(0.25)
    raise AssertionError(f"Tiempo agotado esperando: {what}")


def ffprobe_json(ffprobe: str, target: str) -> dict:
    args = [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height", "-of", "json"]
    if target.startswith("rtsp://"):
        args[1:1] = ["-rtsp_transport", "tcp"]
    r = subprocess.run([*args, target], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.fixture
def sim(camsim_factory: Callable[..., CameraSimulator]) -> CameraSimulator:
    return camsim_factory([SimDevice("hik1", "hikvision", channels=1), SimDevice("dah1", "dahua", channels=1)])


@pytest.fixture
async def engine(settings: VmsSettings, app_paths: AppPaths, mediamtx_bin: str) -> AsyncIterator[MediaMtxEngine]:
    eng = MediaMtxEngine(settings, app_paths, mediamtx_bin=Path(mediamtx_bin), disk_guard_interval=3600,
                         watchdog_interval=1.0, backoff=(0.5, 1.0))
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()
    assert eng.process is not None and eng.process.proc is not None
    assert eng.process.proc.returncode is not None, "MediaMTX debe quedar parado (sin huérfanos)"


def make_config(sim: CameraSimulator, creds: CredentialStore, *, dahua_password: str = DEFAULT_PASSWORD
                ) -> tuple[AppConfig, Camera, Camera]:
    hik = Device(name="NVR Hik", vendor="hikvision", kind="nvr", host="127.0.0.1",
                 rtsp_port=sim.device("hik1").port, username="admin")
    dah = Device(name="NVR Dahua", vendor="dahua", kind="nvr", host="127.0.0.1",
                 rtsp_port=sim.device("dah1").port, username="admin")
    ch = Camera(name="Entrada", device_id=hik.id, channel=1)
    cd = Camera(name="Cajas", device_id=dah.id, channel=1)
    creds.set_device_password(hik.id, DEFAULT_PASSWORD)
    creds.set_device_password(dah.id, dahua_password)
    return AppConfig(devices=[hik, dah], cameras=[ch, cd]), ch, cd


async def ready(engine: MediaMtxEngine, name: str) -> bool:
    st = (await engine.paths_status()).get(name)
    return bool(st and st.ready)


async def test_records_lists_and_serves_valid_mp4(engine: MediaMtxEngine, sim: CameraSimulator,
                                                  credential_store: CredentialStore, ffprobe_bin: str,
                                                  tmp_path: Path) -> None:
    cfg, ch, cd = make_config(sim, credential_store)
    await engine.apply(build_camera_sources(cfg, credential_store), REC, RET, engine.recordings_dir)
    for cam in (ch, cd):
        await wait_for(lambda c=cam: ready(engine, f"{c.id}/main"), 20, f"{cam.name} con vídeo")

    # Ninguna contraseña en el YAML generado (las rutas viajan por la API)
    yml = engine.config_file.read_text(encoding="utf-8")
    assert "Sim#Pass" not in yml and "Sim%23Pass" not in yml and "paths: {}" in yml

    async def recording_h264() -> bool:
        m = (await engine.paths_status()).get(f"{ch.id}/main")
        return bool(m and m.recording and m.tracks == ["H264"])

    await wait_for(recording_h264, 20, "grabación activa en H.264")
    assert (await engine.paths_status())[f"{ch.id}/sub"].ready is False  # bajo demanda: nadie lo está viendo

    async def has_recording(cam: Camera) -> bool:
        spans = await engine.list_recordings(cam.id, None, None)
        return bool(spans) and sum(s.duration for s in spans) >= 3

    for cam in (ch, cd):
        await wait_for(lambda c=cam: has_recording(c), 30, f"tramos grabados de {cam.name}")
        spans = await engine.list_recordings(cam.id, None, None)
        assert all(s.start.tzinfo is not None and s.start.utcoffset().total_seconds() == 0 for s in spans)
        url = engine.playback_get_url(cam.id, spans[0].start, 2.0, "mp4")
        async with httpx.AsyncClient(timeout=20) as client:
            anon = await client.get(url)
            r = await client.get(url, auth=engine.http_credentials())
        assert anon.status_code == 401  # la reproducción de MediaMTX no admite peticiones anónimas
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        out = tmp_path / f"{cam.id}.mp4"
        out.write_bytes(r.content)
        info = ffprobe_json(ffprobe_bin, str(out))
        assert info["streams"][0]["codec_name"] == "h264"
        assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (640, 360)
        assert 1.5 <= float(info["format"]["duration"]) <= 2.5
        files = list((Path(engine.recordings_dir) / cam.id / "main").glob("*.mp4"))
        assert files, "MediaMTX debe escribir segmentos fMP4 en <grabaciones>/<cámara>/main"

    # Fuera de rango: lista vacía, no error
    assert await engine.list_recordings(ch.id, spans[0].start.replace(year=2020), spans[0].start.replace(year=2020,
                                                                                                          hour=1)) == []

    # Subflujo bajo demanda: se abre al leerlo (una conexión) y llega en 320x180
    plain = engine.rtsp_read_url(cd.id, "sub")
    assert "@" not in plain  # la URL que viaja en la configuración no lleva credenciales
    anon = subprocess.run([ffprobe_bin, "-rtsp_transport", "tcp", "-v", "error", plain], capture_output=True,
                          text=True, timeout=30)
    assert anon.returncode != 0 and "401" in anon.stderr  # sin el usuario de lectura no se ve nada
    info = ffprobe_json(ffprobe_bin, with_reader_credentials(plain, engine.mtx_credentials))
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (320, 180)
    disk = await engine.disk_usage()
    assert disk.total > 0 and 0 < disk.percent < 100


async def test_reconnects_after_stream_kill_and_device_offline(engine: MediaMtxEngine, sim: CameraSimulator,
                                                               credential_store: CredentialStore) -> None:
    cfg, ch, _ = make_config(sim, credential_store)
    await engine.apply(build_camera_sources(cfg, credential_store), REC, RET, engine.recordings_dir)
    main = f"{ch.id}/main"
    await wait_for(lambda: ready(engine, main), 20, "vídeo inicial")

    sim.kill_stream("hik1", 1, "main")

    async def down() -> bool:
        return not await ready(engine, main)

    await wait_for(down, 20, "detectar el corte del flujo")
    sim.start_stream("hik1", 1, "main")
    took = await wait_for(lambda: ready(engine, main), 30, "reconexión tras relanzar el flujo")
    print(f"reconexión tras relanzar el flujo: {took:.1f} s")

    sim.set_device_online("hik1", False)
    await wait_for(down, 20, "detectar el equipo apagado")

    async def has_error() -> bool:
        return bool((await engine.paths_status())[main].last_error)

    await wait_for(has_error, 15, "error legible del equipo apagado")
    sim.set_device_online("hik1", True)
    took = await wait_for(lambda: ready(engine, main), 30, "reconexión tras encender el equipo")
    print(f"reconexión tras encender el equipo: {took:.1f} s")
    assert (await engine.paths_status())[main].last_error == ""
    assert (await engine.status()).restarts == 0  # MediaMTX no se reinició: se reconectó la fuente


async def test_supervisor_relaunches_mediamtx_and_reregisters(engine: MediaMtxEngine, sim: CameraSimulator,
                                                              credential_store: CredentialStore) -> None:
    cfg, ch, cd = make_config(sim, credential_store)
    await engine.apply(build_camera_sources(cfg, credential_store), REC, RET, engine.recordings_dir)
    await wait_for(lambda: ready(engine, f"{ch.id}/main"), 20, "vídeo inicial")
    old_pid = (await engine.status()).pid
    assert engine.process is not None
    engine.process.kill_for_tests()

    async def back() -> bool:
        st = await engine.status()
        if not (st.running and st.api_ok and st.restarts == 1):
            return False
        try:
            return await ready(engine, f"{ch.id}/main") and await ready(engine, f"{cd.id}/main")
        except EngineUnavailable:
            return False

    took = await wait_for(back, 30, "MediaMTX relanzado con las rutas registradas de nuevo")
    print(f"MediaMTX relanzado y cámaras con vídeo en {took:.1f} s")
    st = await engine.status()
    assert st.pid != old_pid and st.last_error == ""


async def test_hot_apply_add_change_remove_and_retention(engine: MediaMtxEngine, sim: CameraSimulator,
                                                         credential_store: CredentialStore,
                                                         settings: VmsSettings) -> None:
    cfg, ch, cd = make_config(sim, credential_store)
    yml_at_start = engine.config_file.read_text(encoding="utf-8")
    only_hik = cfg.model_copy(update={"cameras": [ch]})
    await engine.apply(build_camera_sources(only_hik, credential_store), REC, RET, engine.recordings_dir)
    await wait_for(lambda: ready(engine, f"{ch.id}/main"), 20, "primera cámara")
    pid = (await engine.status()).pid

    await engine.apply(build_camera_sources(cfg, credential_store), REC, RET, engine.recordings_dir)
    await wait_for(lambda: ready(engine, f"{cd.id}/main"), 20, "cámara añadida en caliente")

    no_rec = cfg.model_copy(deep=True)
    no_rec.cameras[0].record = False
    new_ret = RetentionSettings(days=3, disk_guard_percent=0)
    await engine.apply(build_camera_sources(no_rec, credential_store), REC, new_ret, engine.recordings_dir)

    async def not_recording() -> bool:
        st = (await engine.paths_status()).get(f"{ch.id}/main")
        return bool(st and st.ready and not st.recording)

    await wait_for(not_recording, 20, "grabación desactivada en caliente")
    async with httpx.AsyncClient(base_url=f"http://{settings.mtx_api_address}", timeout=5,
                                 auth=engine.http_credentials()) as api:
        pd = (await api.get("/v3/config/pathdefaults/get")).json()
        assert pd["recordDeleteAfter"] in ("72h", "3d")  # MediaMTX normaliza la duración
        conf = (await api.get(f"/v3/config/paths/get/{ch.id}/main")).json()
        assert conf.get("record") is False, {k: v for k, v in conf.items() if k != "source"}
    # Con MediaMTX en marcha el YAML no se toca (si cambiara, MediaMTX lo recargaría y perdería las
    # rutas de la API); los ajustes nuevos se escriben antes del siguiente arranque.
    assert engine.config_file.read_text(encoding="utf-8") == yml_at_start

    await engine.apply(build_camera_sources(only_hik, credential_store), REC, new_ret, engine.recordings_dir)
    st = await engine.paths_status()
    assert f"{cd.id}/main" not in st and f"{cd.id}/sub" not in st
    assert (await engine.status()).pid == pid, "los cambios no deben reiniciar MediaMTX"

    # Tras un reinicio inesperado, MediaMTX arranca ya con los ajustes nuevos
    assert engine.process is not None
    engine.process.kill_for_tests()

    async def relaunched() -> bool:
        s = await engine.status()
        return s.running and s.api_ok and s.restarts == 1 and await ready(engine, f"{ch.id}/main")

    await wait_for(relaunched, 30, "relanzamiento con los ajustes nuevos")
    assert "recordDeleteAfter: 72h" in engine.config_file.read_text(encoding="utf-8")


async def test_bad_password_pauses_device_then_resumes(engine: MediaMtxEngine, sim: CameraSimulator,
                                                       credential_store: CredentialStore) -> None:
    cfg, ch, cd = make_config(sim, credential_store, dahua_password="mala")
    await engine.apply(build_camera_sources(cfg, credential_store), REC, RET, engine.recordings_dir)
    await wait_for(lambda: ready(engine, f"{ch.id}/main"), 20, "la cámara buena sigue con vídeo")

    async def paused() -> bool:
        return bool(engine.paused_devices)

    await wait_for(paused, 20, "pausa del equipo con contraseña mala")
    conns = sim.proxy_connections("dah1")
    await asyncio.sleep(6)  # MediaMTX reintentaría cada 5 s si no estuviera en pausa
    assert sim.proxy_connections("dah1") == conns, "no se debe insistir con una contraseña rechazada"
    st = await engine.paths_status()
    assert "pausó" in st[f"{cd.id}/main"].last_error and st[f"{ch.id}/main"].ready

    credential_store.set_device_password(cfg.devices[1].id, DEFAULT_PASSWORD)
    await engine.apply(build_camera_sources(cfg, credential_store), REC, RET, engine.recordings_dir)
    await wait_for(lambda: ready(engine, f"{cd.id}/main"), 20, "reanudación con la contraseña corregida")
    assert engine.paused_devices == []


async def test_second_engine_on_same_ports_fails_clearly(engine: MediaMtxEngine, settings: VmsSettings,
                                                         app_paths: AppPaths, mediamtx_bin: str,
                                                         tmp_path: Path) -> None:
    other = MediaMtxEngine(settings, AppPaths(tmp_path / "otro").ensure(), mediamtx_bin=Path(mediamtx_bin))
    with pytest.raises(EngineUnavailable, match="ocupado"):
        await other.start()
    await other.stop()
    assert (await engine.status()).running
