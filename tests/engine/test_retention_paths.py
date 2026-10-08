"""Retención de cámaras que ya no graban (motor, sin procesos externos).

MediaMTX solo borra por antigüedad las rutas que tiene configuradas: una cámara borrada, desactivada o en pausa por
contraseña rechazada salía de mediamtx.yml y sus grabaciones no caducaban nunca (RGPD). Ahora sus carpetas llevan una
ruta inerte (sin origen ni grabación) que hereda `recordDeleteAfter`.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.engine.recording_doubles import CAM, REC, RET, SRC, api_of, make_engine, now, segment
from vms.core.config_store import ConfigStore
from vms.core.credentials import CredentialStore
from vms.core.models import AppConfig, Camera, Device
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine
from vms.engine.mtx_config import RETENTION_ONLY_CONF, retention_paths
from vms.engine.service import write_engine_config


@pytest.fixture
def eng(settings: VmsSettings, app_paths: AppPaths) -> MediaMtxEngine:
    return make_engine(settings, app_paths)


def test_retention_paths_cover_folders_without_an_active_path(tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    for cam in ("cam-borrada1", "cam-activa01", "cam-sinmain1"):
        (rec / cam).mkdir(parents=True)
    (rec / "cam-borrada1" / "main").mkdir()
    (rec / "cam-activa01" / "main").mkdir()
    (rec / "No-Es-Camara!" / "main").mkdir(parents=True)
    (rec / "suelto.txt").write_text("x")
    out = retention_paths(rec, {"cam-activa01/main": {}, "cam-activa01/sub": {}})
    assert out == {"cam-borrada1/main": {"source": "publisher", "record": False}}
    assert retention_paths(tmp_path / "no-existe", {}) == {}


async def test_attach_yaml_keeps_deleted_and_paused_cameras_under_retention(eng: MediaMtxEngine) -> None:
    segment(eng, now() - timedelta(days=1))
    segment(eng, now() - timedelta(days=40), cam="cam-borrada1")
    await eng.apply([SRC], REC, RET, eng.recordings_dir)
    doc = yaml.safe_load(eng.config_file.read_text(encoding="utf-8"))
    assert doc["paths"]["cam-borrada1/main"] == RETENTION_ONLY_CONF
    assert doc["paths"][f"{CAM}/main"]["source"] == SRC.main_url
    assert doc["pathDefaults"]["recordDeleteAfter"] == "720h"      # la hereda la ruta inerte
    # Contraseña rechazada: la cámara sale del YAML, pero sus grabaciones siguen cubiertas y sin credenciales
    for _ in range(2):
        eng._auth.failure(SRC.main_url)
    async with eng._lock:
        await eng._write_attach_yaml()
    text = eng.config_file.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert doc["paths"][f"{CAM}/main"] == RETENTION_ONLY_CONF and f"{CAM}/sub" not in doc["paths"]
    assert "Cl4ve" not in text


async def test_child_mode_replaces_a_paused_path_with_the_inert_one(eng: MediaMtxEngine) -> None:
    eng.mode = "child"
    eng.process = SimpleNamespace(running=True)  # type: ignore[assignment]
    api = api_of(eng)
    segment(eng, now() - timedelta(days=1))
    api.config_names = {f"{CAM}/main", f"{CAM}/sub", "cam-sincarp1/main"}
    for _ in range(2):
        eng._auth.failure(SRC.main_url)
    async with eng._lock:
        await eng._sync_locked()
    assert api.replaced == {f"{CAM}/main": RETENTION_ONLY_CONF}
    assert sorted(api.deleted) == sorted(["cam-sincarp1/main", f"{CAM}/sub"])


def test_engine_config_includes_retention_paths(settings: VmsSettings, app_paths: AppPaths,
                                                credential_store: CredentialStore) -> None:
    dev = Device(name="NVR", vendor="hikvision", kind="nvr", host="192.0.2.10", username="admin")
    cam = Camera(name="Entrada", device_id=dev.id, channel=1, enabled=False)
    ConfigStore(app_paths.config_file).save(AppConfig(devices=[dev], cameras=[cam]))
    credential_store.set_device_password(dev.id, "Sim#Pass")
    os.makedirs(app_paths.recordings_dir / cam.id / "main")
    file, _, _ = write_engine_config(settings, app_paths)
    doc = yaml.safe_load(file.read_text(encoding="utf-8"))
    assert doc["paths"] == {f"{cam.id}/main": RETENTION_ONLY_CONF}
