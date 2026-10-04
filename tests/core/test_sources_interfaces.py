from __future__ import annotations

from vms.core.credentials import CredentialStore
from vms.core.interfaces import DeviceClient, Engine
from vms.core.models import AppConfig, Camera, Device
from vms.core.sources import build_camera_sources

from tests.fakes import FakeDeviceClient, FakeEngine


def test_build_sources_for_each_vendor(credential_store: CredentialStore) -> None:
    hik = Device(name="Hik", vendor="hikvision", kind="nvr", host="10.0.0.2", username="admin")
    dah = Device(name="Dah", vendor="dahua", host="10.0.0.3", rtsp_port=8554, username="admin")
    gen = Device(name="Gen", vendor="generic", host="cam.local", username="")
    off = Device(name="Off", vendor="hikvision", host="10.0.0.9", enabled=False)
    credential_store.set_device_password(hik.id, "a@b:c")
    credential_store.set_device_password(dah.id, "x")
    cams = [Camera(name="H2", device_id=hik.id, channel=2),
            Camera(name="D1", device_id=dah.id, channel=1, record=False),
            Camera(name="G", device_id=gen.id, main_path="/live", has_sub=False),
            Camera(name="Sin ruta", device_id=gen.id),
            Camera(name="Off", device_id=off.id),
            Camera(name="Deshabilitada", device_id=hik.id, enabled=False)]
    cfg = AppConfig(devices=[hik, dah, gen, off], cameras=cams)
    sources = {s.name: s for s in build_camera_sources(cfg, credential_store)}
    assert set(sources) == {"H2", "D1", "G"}
    assert sources["H2"].main_url == "rtsp://admin:a%40b%3Ac@10.0.0.2:554/Streaming/Channels/201"
    assert sources["H2"].sub_url == "rtsp://admin:a%40b%3Ac@10.0.0.2:554/Streaming/Channels/202"
    assert sources["D1"].main_url.endswith(":8554/cam/realmonitor?channel=1&subtype=0")
    assert sources["D1"].record is False
    assert sources["G"].main_url == "rtsp://cam.local:554/live" and sources["G"].sub_url is None
    assert "a%40b" not in repr(sources["H2"]) and "a%40b" not in str(sources["H2"])


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeEngine(), Engine)
    assert isinstance(FakeDeviceClient(), DeviceClient)
