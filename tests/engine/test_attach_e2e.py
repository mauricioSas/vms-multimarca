"""Motor como servicio aparte (modo `attach`, CONTRATO §13.10) con MediaMTX y el simulador de cámaras reales.

MediaMTX lo lanza `vms.engine.service.run_engine` (lo mismo que hace `vmsctl run --service VMSEngine` en
Windows: YAML como fuente única y salida sin credenciales en `logs/engine.log`); el «backend» es
`MediaMtxEngine(mode="attach")`. Criterio (3) de B1 en PLAN-V2 §6.2:

- backend reiniciado 5 veces con el motor aparte → **0 huecos** en `/list` y ninguna ruta reiniciada;
- motor reiniciado con el backend parado → vuelve a grabar solo, con el YAML.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterator

import psutil
import pytest

from tools.camsim.simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice
from vms.core.credentials import CredentialStore
from vms.core.interfaces import CameraSource
from vms.core.models import AppConfig, Camera, Device, RecordingSettings, RetentionSettings
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.core.sources import build_camera_sources
from vms.engine import MediaMtxEngine
from vms.engine.service import run_engine

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg]

REC = RecordingSettings(segment_seconds=60, part_seconds=1)
RET = RetentionSettings(days=2, disk_guard_percent=0)


async def wait_for(check: Callable[[], Awaitable[bool]], timeout: float, what: str) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if await check():
                return time.monotonic() - t0
        except Exception:  # noqa: BLE001 - el motor puede estar arrancando
            pass
        await asyncio.sleep(0.25)
    raise AssertionError(f"Tiempo agotado esperando: {what}")


@pytest.fixture
def sim(camsim_factory: Callable[..., CameraSimulator]) -> CameraSimulator:
    return camsim_factory([SimDevice("hik1", "hikvision", channels=1), SimDevice("dah1", "dahua", channels=1)])


@pytest.fixture
def engine_service(app_paths: AppPaths, mediamtx_bin: str) -> Iterator[threading.Event]:
    """El «servicio VMSEngine»: MediaMTX con el YAML, relanzado si cae. Se para al acabar."""
    stop = threading.Event()
    t = threading.Thread(target=run_engine, args=(app_paths, Path(mediamtx_bin), stop),
                         kwargs={"backoff": (0.5, 1.0)}, daemon=True, name="vmsengine")
    t.start()
    yield stop
    stop.set()
    t.join(15)
    assert not t.is_alive(), "el servicio del motor debe parar"


def backend(settings: VmsSettings, app_paths: AppPaths) -> MediaMtxEngine:
    return MediaMtxEngine(settings, app_paths, mode="attach", disk_guard_interval=3600, attach_poll=0.5,
                          logtail_interval=0.2, watchdog_interval=2.0)


@pytest.fixture
async def eng(settings: VmsSettings, app_paths: AppPaths, engine_service: threading.Event
              ) -> AsyncIterator[MediaMtxEngine]:
    e = backend(settings, app_paths)
    await e.start()
    try:
        yield e
    finally:
        await e.stop()


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


def make_sources(sim: CameraSimulator, creds: CredentialStore) -> tuple[list[CameraSource], Camera, Camera]:
    cfg, ch, cd = make_config(sim, creds)
    return build_camera_sources(cfg, creds), ch, cd


async def ready(engine: MediaMtxEngine, name: str) -> bool:
    st = (await engine.paths_status()).get(name)
    return bool(st and st.ready)


async def ready_times(engine: MediaMtxEngine) -> dict[str, str]:
    assert engine.api is not None
    return {str(i["name"]): str(i.get("readyTime")) for i in await engine.api.paths_list()
            if str(i.get("name", "")).endswith("/main")}


def mediamtx_pids(yml: Path) -> list[int]:
    out = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(str(yml) == a for a in (p.info["cmdline"] or [])):
                out.append(p.pid)
        except psutil.Error:
            continue
    return out


async def test_backend_restarted_5_times_leaves_no_gap(eng: MediaMtxEngine, sim: CameraSimulator,
                                                       credential_store: CredentialStore, settings: VmsSettings,
                                                       app_paths: AppPaths) -> None:
    sources, ch, cd = make_sources(sim, credential_store)
    await eng.apply(sources, REC, RET, eng.recordings_dir)
    took = await wait_for(lambda: ready(eng, f"{ch.id}/main"), 30, "el servicio del motor lee el YAML y graba")
    await wait_for(lambda: ready(eng, f"{cd.id}/main"), 30, "segunda cámara")
    print(f"YAML escrito → vídeo en {took:.1f} s")
    yml_stat = eng.config_file.stat()
    before = await ready_times(eng)
    t_first = time.time()
    await asyncio.sleep(3)

    current = eng
    for i in range(5):
        await current.stop()                       # el backend se para (actualización, caída…)
        await asyncio.sleep(1.0)                   # …unos instantes sin backend: el motor sigue grabando
        current = backend(settings, app_paths)
        await current.start()
        await current.apply(sources, REC, RET, current.recordings_dir)
        assert current.running, f"reinicio {i + 1}: el backend ve el motor"
        await asyncio.sleep(1.5)
    await asyncio.sleep(3)
    try:
        after = await ready_times(current)
        assert after == before, "ninguna ruta de MediaMTX se reinició con los reinicios del backend"
        st = eng.config_file.stat()
        assert (st.st_mtime_ns, st.st_ino) == (yml_stat.st_mtime_ns, yml_stat.st_ino), \
            "el YAML no se reescribió (MediaMTX no recargó)"
        elapsed = time.time() - t_first
        for cam in (ch, cd):
            spans = await current.list_recordings(cam.id, None, None)
            gaps = [round((b.start - a.start).total_seconds() - a.duration, 3) for a, b in itertools.pairwise(spans)]
            total = sum(s.duration for s in spans)
            print(f"{cam.name}: {len(spans)} tramo(s), {total:.1f} s grabados, huecos {gaps}")
            assert all(g <= 0.1 for g in gaps), f"hueco en la grabación de {cam.name}: {gaps}"
            assert total >= elapsed - 3, f"{cam.name}: {total:.1f} s grabados en {elapsed:.1f} s"
    finally:
        await current.stop()


async def test_engine_restarted_with_backend_stopped_records_again(eng: MediaMtxEngine, sim: CameraSimulator,
                                                                   credential_store: CredentialStore,
                                                                   settings: VmsSettings,
                                                                   app_paths: AppPaths) -> None:
    sources, ch, cd = make_sources(sim, credential_store)
    await eng.apply(sources, REC, RET, eng.recordings_dir)
    for cam in (ch, cd):
        await wait_for(lambda c=cam: ready(eng, f"{c.id}/main"), 30, f"{cam.name} grabando")
    await eng.stop()                                # backend parado

    pids = mediamtx_pids(eng.config_file)
    assert len(pids) == 1, pids
    killed_at = time.time()
    psutil.Process(pids[0]).kill()                  # el motor cae con el backend parado

    def new_segments() -> bool:
        for cam in (ch, cd):
            files = list((Path(eng.recordings_dir) / cam.id / "main").glob("*.mp4"))
            if not any(f.stat().st_mtime > killed_at + 0.5 and f.stat().st_ctime > killed_at for f in files):
                return False
        return True

    t0 = time.monotonic()
    while not new_segments():
        assert time.monotonic() - t0 < 30, "el motor no volvió a grabar solo con el YAML"
        await asyncio.sleep(0.25)
    print(f"motor relanzado y grabando sin backend en {time.monotonic() - t0:.1f} s")
    assert mediamtx_pids(eng.config_file) not in ([], pids)

    # El backend vuelve y ve el motor en marcha con las dos cámaras
    back = backend(settings, app_paths)
    await back.start()
    try:
        await back.apply(sources, REC, RET, back.recordings_dir)
        for cam in (ch, cd):
            await wait_for(lambda c=cam: ready(back, f"{c.id}/main"), 15, f"{cam.name} visto por el backend")
    finally:
        await back.stop()


async def test_bad_password_is_detected_from_engine_log_and_paused(eng: MediaMtxEngine, sim: CameraSimulator,
                                                                   credential_store: CredentialStore) -> None:
    cfg, ch, cd = make_config(sim, credential_store, dahua_password="mala")
    await eng.apply(build_camera_sources(cfg, credential_store), REC, RET, eng.recordings_dir)
    await wait_for(lambda: ready(eng, f"{ch.id}/main"), 30, "la cámara buena graba")

    async def paused() -> bool:
        return bool(eng.paused_devices) and f"{cd.id}/main" not in eng.config_file.read_text(encoding="utf-8")

    await wait_for(paused, 30, "pausa del equipo con la contraseña mala (vía engine.log)")
    conns = sim.proxy_connections("dah1")
    await asyncio.sleep(6)   # MediaMTX reintentaría cada 5 s si la ruta siguiera en el YAML
    assert sim.proxy_connections("dah1") == conns, "no se insiste con una contraseña rechazada"
    st = await eng.paths_status()
    assert "pausó" in st[f"{cd.id}/main"].last_error and st[f"{ch.id}/main"].ready
    log_text = (eng.paths.logs_dir / "engine.log").read_text(encoding="utf-8")
    assert "mala" not in log_text and DEFAULT_PASSWORD not in log_text, "engine.log sin contraseñas"

    # Contraseña corregida → vuelve al YAML y graba
    credential_store.set_device_password(cfg.devices[1].id, DEFAULT_PASSWORD)
    await eng.apply(build_camera_sources(cfg, credential_store), REC, RET, eng.recordings_dir)
    await wait_for(lambda: ready(eng, f"{cd.id}/main"), 30, "reanudación con la contraseña buena")
    assert eng.paused_devices == []
