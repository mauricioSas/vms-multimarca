from __future__ import annotations

import pytest
from pydantic import ValidationError

from vms.core.models import (AppConfig, Camera, CameraAnalytics, Device, DeviceCreate, LineRule, UserCreate,
                             WallLayout, ZoneRule)


def make_config() -> tuple[AppConfig, Device, Camera, Camera]:
    dev = Device(name="NVR Entrada", vendor="hikvision", kind="nvr", host="192.168.1.20", username="admin")
    c1 = Camera(name="Puerta", device_id=dev.id, channel=1)
    c2 = Camera(name="Cajas", device_id=dev.id, channel=2)
    cfg = AppConfig(devices=[dev], cameras=[c1, c2])
    cfg.walls[0].cells[0] = c1.id
    cfg.walls[1].cells[3] = c2.id
    cfg.analytics_rules = [LineRule(camera_id=c1.id, name="Puerta", start=(0.1, 0.5), end=(0.9, 0.5)),
                           ZoneRule(camera_id=c2.id, name="Cola", polygon=[(0, 0), (1, 0), (1, 1)])]
    cfg.analytics_cameras = [CameraAnalytics(camera_id=c1.id, enabled=True, fps=12)]
    return cfg, dev, c1, c2


def test_device_validation_messages_in_spanish() -> None:
    with pytest.raises(ValidationError) as e:
        Device(name="x", vendor="hikvision", host="rtsp://192.168.1.2/")
    assert "sin rtsp://" in str(e.value)
    with pytest.raises(ValidationError):
        Device(name="x", vendor="dahua", host="192.168.1.300")
    with pytest.raises(ValidationError):
        Device(name="x", vendor="dahua", host="10.0.0.1", username="a:b")


def test_device_create_password_is_secret() -> None:
    d = DeviceCreate(name="Cam", vendor="dahua", host="10.0.0.3", username="admin", password="Secreta#1")
    assert "Secreta" not in repr(d)
    assert "password" not in Device.model_fields


def test_camera_paths_normalized_and_validated() -> None:
    c = Camera(name="x", device_id="dev-12345678", main_path="Streaming/Channels/101", sub_path="  ")
    assert c.main_path == "/Streaming/Channels/101" and c.sub_path is None
    with pytest.raises(ValidationError):
        Camera(name="x", device_id="dev-12345678", main_path="rtsp://h/x")


def test_wall_layout_always_16_cells() -> None:
    w = WallLayout(monitor=2, grid=9, cells=["cam-12345678"])
    assert len(w.cells) == 16 and w.cells[0] == "cam-12345678"
    assert len(w.visible_cells()) == 9
    with pytest.raises(ValidationError):
        WallLayout(monitor=5)
    with pytest.raises(ValidationError):
        WallLayout(monitor=1, grid=6)  # type: ignore[arg-type]


def test_rules_validation() -> None:
    with pytest.raises(ValidationError):
        LineRule(camera_id="cam-12345678", name="x", start=(0.5, 0.5), end=(0.5, 0.5))
    with pytest.raises(ValidationError):
        LineRule(camera_id="cam-12345678", name="x", start=(-0.1, 0.5), end=(0.5, 0.5))
    with pytest.raises(ValidationError):
        ZoneRule(camera_id="cam-12345678", name="x", polygon=[(0, 0), (1, 1)])


def test_config_roundtrip_with_discriminated_rules() -> None:
    cfg, *_ = make_config()
    again = AppConfig.model_validate_json(cfg.model_dump_json())
    kinds = sorted(r.kind for r in again.analytics_rules)
    assert kinds == ["line", "zone"]
    assert isinstance(again.analytics_rules[0], (LineRule, ZoneRule))


def test_remove_device_cascades() -> None:
    cfg, dev, c1, c2 = make_config()
    removed = cfg.remove_device(dev.id)
    assert set(removed) == {c1.id, c2.id}
    assert cfg.devices == [] and cfg.cameras == []
    assert all(c is None for w in cfg.walls for c in w.cells)
    assert cfg.analytics_rules == [] and cfg.analytics_cameras == []


def test_repair_drops_dangling_references() -> None:
    cfg, dev, c1, _ = make_config()
    cfg.cameras.append(Camera(name="Huérfana", device_id="dev-00000000"))
    cfg.walls[2].cells[0] = "cam-99999999"
    problems = cfg.repair()
    assert any("Huérfana" in p for p in problems)
    assert cfg.walls[2].cells[0] is None
    assert len(cfg.walls) == 4


def test_user_create_rules() -> None:
    with pytest.raises(ValidationError):
        UserCreate(username="ab", password="12345678")
    with pytest.raises(ValidationError):
        UserCreate(username="operador1", password="corta")
    assert UserCreate(username="operador.1", password="larga-y-buena").role == "operator"
