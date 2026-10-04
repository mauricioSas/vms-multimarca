"""Prueba RTSP propia (DESCRIBE + Digest) y test_device() contra el simulador de cámaras real."""
from __future__ import annotations

from typing import Any, Callable

import pytest

from tests.conftest import get_free_port
from tools.camsim.simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice
from tools.mocks.dahua import DahuaChannel, DahuaMock
from tools.mocks.hikvision import HikChannel, HikvisionMock
from vms.core.models import DeviceTestRequest
from vms.vendors import probe_rtsp, test_device as run_device_test

pytestmark = [pytest.mark.e2e, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg]


@pytest.fixture
def sim(camsim_factory: Callable[..., CameraSimulator]) -> CameraSimulator:
    return camsim_factory([SimDevice("hik1", "hikvision", channels=1), SimDevice("dah1", "dahua", channels=2)])


async def test_rtsp_describe_with_digest(sim: CameraSimulator) -> None:
    hik, dah = sim.device("hik1"), sim.device("dah1")
    ok = await probe_rtsp("127.0.0.1", hik.port, "/Streaming/Channels/101", "admin", DEFAULT_PASSWORD)
    assert ok.ok and ok.reachable and ok.auth_ok and ok.video_codec == "H.264"
    ok2 = await probe_rtsp("127.0.0.1", dah.port, "/cam/realmonitor?channel=2&subtype=1", "admin", DEFAULT_PASSWORD)
    assert ok2.ok and ok2.codecs == ["H264"]
    bad = await probe_rtsp("127.0.0.1", hik.port, "/Streaming/Channels/101", "admin", "mala")
    assert bad.status == 401 and bad.auth_ok is False and "incorrectos" in bad.error
    missing = await probe_rtsp("127.0.0.1", hik.port, "/Streaming/Channels/901", "admin", DEFAULT_PASSWORD)
    assert missing.status == 404 and "no existe" in missing.error
    closed = await probe_rtsp("127.0.0.1", get_free_port(), "/x", timeout=2)
    assert not closed.reachable and closed.error


async def test_test_device_full_check_hikvision(sim: CameraSimulator, mock_server: Callable[[Any], Any]) -> None:
    api = HikvisionMock(password=DEFAULT_PASSWORD, channels=[HikChannel("Entrada")])
    srv = mock_server(api.app)
    req = DeviceTestRequest(vendor="hikvision", kind="nvr", host="127.0.0.1", http_port=srv.port,
                            rtsp_port=sim.device("hik1").port, username="admin")
    res = await run_device_test(req, DEFAULT_PASSWORD)
    assert res.ok and res.reachable and res.auth_ok and res.rtsp_ok, res.message
    assert res.info is not None and res.info.kind == "nvr" and len(res.channels) == 1
    assert "Vídeo RTSP correcto" in res.message


async def test_test_device_dahua_and_failures(sim: CameraSimulator, mock_server: Callable[[Any], Any]) -> None:
    api = DahuaMock(password=DEFAULT_PASSWORD, channels=[DahuaChannel("Entrada"), DahuaChannel("Cajas")])
    srv = mock_server(api.app)
    base = dict(vendor="dahua", kind="nvr", host="127.0.0.1", http_port=srv.port,
                rtsp_port=sim.device("dah1").port, username="admin")
    res = await run_device_test(DeviceTestRequest(**base), DEFAULT_PASSWORD)  # type: ignore[arg-type]
    assert res.ok and res.rtsp_ok and len(res.channels) == 2, res.message

    before = api.auth.failures
    bad = await run_device_test(DeviceTestRequest(**base), "mala")  # type: ignore[arg-type]
    assert not bad.ok and bad.auth_ok is False and bad.rtsp_ok is None
    assert "contraseña" in bad.message
    # reto Digest + un único intento con la contraseña mala; tras el rechazo no se insiste (ni por RTSP)
    assert api.auth.failures - before == 2

    nobody = DeviceTestRequest(vendor="dahua", host="127.0.0.1", http_port=get_free_port(), rtsp_port=get_free_port())
    off = await run_device_test(nobody, "x", timeout=2)
    assert not off.ok and not off.reachable and "No se puede conectar" in off.message


async def test_test_device_generic_rtsp_only(sim: CameraSimulator) -> None:
    req = DeviceTestRequest(vendor="generic", host="127.0.0.1", rtsp_port=sim.device("hik1").port)
    res = await run_device_test(req, "")
    assert res.ok and res.reachable and res.info is None
    assert "ruta RTSP" in res.message
