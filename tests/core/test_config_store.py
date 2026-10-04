from __future__ import annotations

import json
from pathlib import Path

import pytest

from vms.core.config_store import ConfigRepository, ConfigStore, UserStore
from vms.core.models import AppConfig, Camera, Device, User
from vms.core.paths import AppPaths


def _device() -> Device:
    return Device(name="NVR", vendor="dahua", kind="nvr", host="10.0.0.5", username="admin")


def test_save_load_and_backup(app_paths: AppPaths) -> None:
    store = ConfigStore(app_paths.config_file)
    cfg = AppConfig(devices=[_device()])
    store.save(cfg)
    assert not store.backup_path.exists()
    cfg.devices[0].name = "NVR 2"
    store.save(cfg)
    assert store.backup_path.exists()
    assert json.loads(store.backup_path.read_text())["devices"][0]["name"] == "NVR"
    loaded, warning = store.load()
    assert warning is None and loaded.devices[0].name == "NVR 2"


def test_corrupt_file_is_quarantined_and_backup_used(app_paths: AppPaths) -> None:
    store = ConfigStore(app_paths.config_file)
    store.save(AppConfig(devices=[_device()]))
    store.save(AppConfig(devices=[_device(), _device()]))
    app_paths.config_file.write_text("{ esto no es json", encoding="utf-8")
    cfg, warning = store.load()
    assert warning and "dañado" in warning and "copia de seguridad" in warning
    assert len(cfg.devices) == 1
    assert list(app_paths.config_dir.glob("config.corrupt-*.json"))


def test_plaintext_password_is_ignored_and_original_kept(app_paths: AppPaths) -> None:
    dev = _device()
    raw = json.loads(AppConfig(devices=[dev]).model_dump_json())
    raw["devices"][0]["password"] = "texto-plano"
    app_paths.config_file.write_text(json.dumps(raw), encoding="utf-8")
    cfg, warning = ConfigStore(app_paths.config_file).load()
    assert warning and "texto plano" in warning
    assert "texto-plano" not in cfg.model_dump_json()
    assert list(app_paths.config_dir.glob("config.original-*.json"))


async def test_repository_update_notifies_and_persists(app_paths: AppPaths) -> None:
    repo = ConfigRepository(ConfigStore(app_paths.config_file))
    seen: list[int] = []

    async def listener(cfg: AppConfig) -> None:
        seen.append(len(cfg.devices))

    def broken(cfg: AppConfig) -> None:
        raise RuntimeError("suscriptor roto")

    repo.subscribe(broken)
    repo.subscribe(listener)
    dev = _device()
    result = await repo.update(lambda c: (c.devices.append(dev), dev.id)[1])
    assert result == dev.id and seen == [1] and repo.revision == 1
    assert ConfigStore(app_paths.config_file).load()[0].devices[0].id == dev.id


async def test_repository_rejects_invalid_change_without_saving(app_paths: AppPaths) -> None:
    repo = ConfigRepository(ConfigStore(app_paths.config_file))

    def bad(cfg: AppConfig) -> None:
        cfg.cameras.append(Camera.model_construct(name="", device_id="x", channel=0))

    with pytest.raises(Exception):
        await repo.update(bad)
    assert repo.config.cameras == [] and not app_paths.config_file.exists()


async def test_user_store_roundtrip(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "users.json")
    await store.save_user(User(username="Admin", role="admin", password_hash="$argon2id$fake"))
    again = UserStore(tmp_path / "users.json")
    assert again.get("admin") is not None and again.get("ADMIN").role == "admin"  # type: ignore[union-attr]
    await again.delete_user("admin")
    assert UserStore(tmp_path / "users.json").all() == []
