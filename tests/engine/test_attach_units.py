"""Modo `attach` del motor sin procesos reales (CONTRATO §13.10): YAML como fuente única, lector de
`engine.log` con rotación, pausa por 401 que quita rutas del YAML, eventos `engine` y `engine-config`."""
from __future__ import annotations

import asyncio
import io
import os
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

import vms.__main__ as vms_main
from vms.core.credentials import CredentialStore
from vms.core.errors import EngineUnavailable
from vms.core.interfaces import CameraSource
from vms.core.models import AppConfig, Camera, Device, RecordingSettings, RetentionSettings
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine
from vms.engine.logtail import LogTail
from vms.engine.mtx_api import MtxApiError
from vms.engine.service import write_engine_config

REC = RecordingSettings(segment_seconds=60, part_seconds=1)
RET = RetentionSettings(days=2, disk_guard_percent=0)


def src(cam: str, password: str = "Cl4ve#1", host: str = "10.0.0.5") -> CameraSource:
    return CameraSource(camera_id=cam, name=cam, main_url=f"rtsp://admin:{password.replace('#', '%23')}@{host}:554/main",
                        sub_url=f"rtsp://admin:{password.replace('#', '%23')}@{host}:554/sub", record=True)


@pytest.fixture
def attach(settings: VmsSettings, app_paths: AppPaths) -> MediaMtxEngine:
    return MediaMtxEngine(settings, app_paths, mode="attach", disk_guard_interval=3600, attach_poll=0.05,
                          logtail_interval=0.05)


# ----------------------------------------------------------------------------------------------- logtail
def test_logtail_starts_at_end_and_returns_only_complete_lines(tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text("2026/10/05 10:00:00 ERR viejo 401\n", encoding="utf-8")
    seen: list[str] = []
    t = LogTail(log, seen.append)
    assert t.poll() == 0, "lo anterior al arranque no se procesa"
    with open(log, "a", encoding="utf-8") as f:
        f.write("línea uno\nlínea a med")
    assert t.poll() == 1 and seen == ["línea uno"]
    with open(log, "a", encoding="utf-8") as f:
        f.write("ias\n")
    t.poll()
    assert seen == ["línea uno", "línea a medias"]


def test_logtail_follows_rotation_without_losing_lines(tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text("", encoding="utf-8")
    seen: list[str] = []
    t = LogTail(log, seen.append)
    t.poll()
    with open(log, "a", encoding="utf-8") as f:
        f.write("a\nb\n")
    t.poll()
    with open(log, "a", encoding="utf-8") as f:
        f.write("c\n")                       # escrita justo antes de rotar, aún sin leer
    os.replace(log, tmp_path / "engine.log.1")  # vmsctl rota
    log.write_text("d\n", encoding="utf-8")
    t.poll()
    assert seen == ["a", "b", "c", "d"] and t.rotations == 1
    log.write_text("", encoding="utf-8")       # truncado (se nota porque encoge)
    t.poll()
    with open(log, "a", encoding="utf-8") as f:
        f.write("e\n")
    t.poll()
    assert seen[-1] == "e"


def test_logtail_follows_copy_truncate_rotation_without_losing_lines(tmp_path: Path) -> None:
    """vmsctl rota engine.log copiando a .1 y vaciando: el archivo es el mismo y encoge."""
    log = tmp_path / "engine.log"
    log.write_text("antes\n", encoding="utf-8")
    seen: list[str] = []
    t = LogTail(log, seen.append)
    t.poll()
    with open(log, "a", encoding="utf-8") as f:
        f.write("a\nb\n")
    t.poll()
    with open(log, "a", encoding="utf-8") as f:
        f.write("c\n")                                   # sin leer todavía
    (tmp_path / "engine.log.1").write_bytes(log.read_bytes())   # copia…
    with open(log, "r+b") as f:                           # …y vaciado del mismo archivo
        f.truncate(0)
    with open(log, "a", encoding="utf-8") as f:
        f.write("d\n")
    t.poll()
    assert seen == ["a", "b", "c", "d"] and t.rotations == 1


def test_logtail_ignores_a_backup_that_does_not_continue_what_was_read(tmp_path: Path) -> None:
    """Una engine.log.1 que no es la copia de lo leído (p. ej. creada por otro) no se procesa."""
    log = tmp_path / "engine.log"
    log.write_text("", encoding="utf-8")
    seen: list[str] = []
    t = LogTail(log, seen.append)
    t.poll()
    with open(log, "a", encoding="utf-8") as f:
        f.write("real 1\nreal 2\n")
    t.poll()
    (tmp_path / "engine.log.1").write_text("ERR [path cam/main] 401 inventado\n" * 5, encoding="utf-8")
    with open(log, "r+b") as f:
        f.truncate(0)
    t.poll()
    assert seen == ["real 1", "real 2"]


def test_logtail_reads_a_file_that_appears_later_from_the_beginning(tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    seen: list[str] = []
    t = LogTail(log, seen.append)
    assert t.poll() == 0
    log.write_text("primera\n", encoding="utf-8")
    t.poll()
    assert seen == ["primera"]


# ----------------------------------------------------------------------------------------------- YAML
async def test_apply_writes_complete_yaml_even_with_the_engine_down(attach: MediaMtxEngine) -> None:
    await attach.start()   # el servicio VMSEngine no responde: el backend arranca igual
    try:
        st = await attach.status()
        assert not st.running and "VMSEngine" in st.last_error
        assert not attach.config_file.exists(), "start() no escribe un YAML vacío (abriría un hueco)"
        await attach.apply([src("cam-aaaa1111"), src("cam-bbbb2222", host="10.0.0.6")], REC, RET,
                           attach.recordings_dir)
        doc = yaml.safe_load(attach.config_file.read_text(encoding="utf-8"))
        assert set(doc["paths"]) == {"cam-aaaa1111/main", "cam-aaaa1111/sub", "cam-bbbb2222/main",
                                     "cam-bbbb2222/sub"}
        assert doc["paths"]["cam-aaaa1111/main"]["source"].startswith("rtsp://admin:Cl4ve%231@")
        assert doc["paths"]["cam-aaaa1111/main"]["record"] is True
        assert doc["pathDefaults"]["recordDeleteAfter"] == "48h"
        assert doc["authInternalUsers"] and "any" not in str(doc["authInternalUsers"])
        assert attach.config_file.read_text(encoding="utf-8").startswith("# Generado por VMS Multimarca (motor")
    finally:
        await attach.stop()


async def test_identical_apply_does_not_touch_the_file(attach: MediaMtxEngine) -> None:
    sources = [src("cam-aaaa1111")]
    await attach.apply(sources, REC, RET, attach.recordings_dir)
    st1 = attach.config_file.stat()
    await asyncio.sleep(0.01)
    # Un «reinicio del backend»: otra instancia con las mismas cámaras no reescribe (MediaMTX no recarga)
    again = MediaMtxEngine(attach.settings, attach.paths, mode="attach")
    await again.apply(sources, REC, RET, attach.recordings_dir)
    st2 = attach.config_file.stat()
    assert (st1.st_mtime_ns, st1.st_ino) == (st2.st_mtime_ns, st2.st_ino)
    await again.apply([], REC, RET, attach.recordings_dir)
    assert yaml.safe_load(attach.config_file.read_text(encoding="utf-8"))["paths"] == {}


async def test_401_in_engine_log_pauses_the_device_and_removes_its_paths(attach: MediaMtxEngine) -> None:
    await attach.start()
    try:
        await attach.apply([src("cam-aaaa1111"), src("cam-bbbb2222", host="10.0.0.6")], REC, RET,
                           attach.recordings_dir)
        log = attach.paths.logs_dir / "engine.log"
        line = ("2026/10/05 10:00:0{} ERR [path cam-bbbb2222/main] [RTSP source] "
                "bad status code: 401 (Unauthorized)\n")
        for i in range(2):   # umbral: 2 rechazos
            with open(log, "a", encoding="utf-8") as f:
                f.write(line.format(i))
            await asyncio.sleep(0.2)
        for _ in range(50):
            if "cam-bbbb2222/main" not in attach.config_file.read_text(encoding="utf-8"):
                break
            await asyncio.sleep(0.05)
        doc = yaml.safe_load(attach.config_file.read_text(encoding="utf-8"))
        assert set(doc["paths"]) == {"cam-aaaa1111/main", "cam-aaaa1111/sub"}
        assert attach.paused_devices and attach.paused_devices[0][0] == "10.0.0.6"
    finally:
        await attach.stop()


class FakeApi:
    """API de MediaMTX que se puede «apagar» y «reiniciar»."""

    def __init__(self) -> None:
        self.up = True
        self.started = "2026-10-05T10:00:00.000000000Z"

    async def info(self) -> dict[str, Any]:
        if not self.up:
            raise EngineUnavailable("no responde")
        return {"version": "v1.21.1", "started": self.started}

    async def aclose(self) -> None:
        return None

    async def paths_list(self) -> list[dict[str, Any]]:
        raise MtxApiError(500, "no hace falta")


async def test_engine_events_and_status_follow_the_service(attach: MediaMtxEngine,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    attach.on_event = events.append
    fake = FakeApi()
    import vms.engine.engine as engmod

    monkeypatch.setattr(engmod, "MediaMtxApi", lambda *a, **k: fake)
    await attach.start()
    try:
        st = await attach.status()
        assert st.running and st.api_ok and st.version == "v1.21.1" and st.started_at is not None
        fake.up = False
        await asyncio.sleep(0.2)
        assert not attach.running
        fake.up = True
        fake.started = "2026-10-05T10:05:00.000000000Z"
        await asyncio.sleep(0.2)
        st = await attach.status()
        assert st.running and st.restarts == 1
        assert [e["state"] for e in events] == ["started", "down", "restarted"]
        assert all(e["at"].endswith("Z") and e["pid"] is None for e in events)
    finally:
        await attach.stop()


# ----------------------------------------------------------------------------------------------- engine-config
def test_engine_config_writes_the_yaml_from_config_and_credentials(settings: VmsSettings, app_paths: AppPaths,
                                                                   credential_store: CredentialStore) -> None:
    dev = Device(name="NVR", vendor="hikvision", kind="nvr", host="192.168.1.64", username="admin")
    cam = Camera(name="Entrada", device_id=dev.id, channel=1)
    from vms.core.config_store import ConfigStore

    ConfigStore(app_paths.config_file).save(AppConfig(devices=[dev], cameras=[cam]))
    credential_store.set_device_password(dev.id, "Sim#Pass")
    file, routes, changed = write_engine_config(settings, app_paths)
    assert (routes, changed) == (2, True)
    text = file.read_text(encoding="utf-8")
    assert "rtsp://admin:Sim%23Pass@192.168.1.64:554/Streaming/Channels/101" in text
    assert write_engine_config(settings, app_paths)[2] is False, "sin cambios no se reescribe"


def test_engine_config_cli(settings: VmsSettings, app_paths: AppPaths, monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("VMS_INTERNAL_TOKEN", "token-interno-de-pruebas")
    monkeypatch.setenv("VMS_CREDENTIAL_BACKEND", "file")
    assert vms_main.main(["engine-config"]) == 0
    assert "0 rutas" in capsys.readouterr().out
    assert (app_paths.mediamtx_dir / "mediamtx.yml").is_file()


# ----------------------------------------------------------------------------------------------- parada por vmsctl
def test_backend_stopped_by_vmsctl_exits_0_without_traceback(monkeypatch: pytest.MonkeyPatch,
                                                            settings: VmsSettings) -> None:
    """Regresión: la parada por EOF de la entrada estándar acababa en KeyboardInterrupt sin capturar
    (traceback en VMSBackend.log y código STATUS_CONTROL_C_EXIT)."""
    import vms.api
    import vms.api.serve
    import vms.core.logging_setup as ls
    import vms.core.settings as st

    def stopped(app: object, s: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(st, "load_settings", lambda *a, **k: settings)
    monkeypatch.setattr(vms.api, "create_app", lambda s: object())
    monkeypatch.setattr(vms.api.serve, "run", stopped)
    monkeypatch.setattr(ls, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(ls, "setup_audit_log", lambda *a, **k: None)
    monkeypatch.delenv(vms_main.STOP_ON_STDIN_EOF, raising=False)
    assert vms_main.main([]) == 0

def test_stdin_eof_simulates_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    raised = threading.Event()

    class FakeStdin:
        buffer = io.BytesIO(b"")   # EOF inmediato: vmsctl cerró la entrada

    monkeypatch.setattr("sys.stdin", FakeStdin())
    monkeypatch.setattr("signal.raise_signal", lambda sig: raised.set())
    vms_main.install_stdin_eof_stop().join(2)
    assert raised.is_set()
