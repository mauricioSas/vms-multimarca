"""`test_device` con el registro: un solo intento con credenciales, bloqueo, Basic, DVR/XVR, firmware, avisos.

La API se sirve con los mocks (sin red, transporte ASGI) y el RTSP con el servidor caótico en un puerto real.
"""
from __future__ import annotations

import asyncio
import logging
import socket

import pytest

from tests.vendors.conftest import TEST_PASSWORD, asgi, make_device
from tools.mocks.dahua import DahuaMock
from tools.mocks.hikvision import HikChannel, HikvisionMock
from tools.mocks.rtsp_chaos import RtspChaosServer
from vms.core.models import DeviceTestRequest
from vms.vendors import test_device as run_test


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_hikvision_nvr_ok_with_warnings(chaos: RtspChaosServer, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    mock = HikvisionMock(password=TEST_PASSWORD)
    r = await run_test(make_device("hikvision", kind="nvr", rtsp_port=chaos.port), TEST_PASSWORD,
                       transport=asgi(mock.app))
    assert r.ok and r.auth_ok and r.rtsp_ok and r.info and r.info.kind == "nvr" and len(r.channels) == 4
    assert r.info.firmware_date == "2021-08-12"
    assert r.first_frame_ms is not None and not r.gop_slow
    assert r.bandwidth_kbps == 3 * (4096 + 512)          # el canal sin vídeo no suma
    assert any("H.265" in w for w in r.warnings)          # principal del canal 4 en H.265
    assert "Entrada estimada" in r.message
    await asyncio.sleep(0.2)
    assert any(x.startswith("TEARDOWN") for x in chaos.stats.requests)     # la sesión de medida se cierra
    assert TEST_PASSWORD not in caplog.text


async def test_wrong_password_sends_credentials_exactly_once(chaos: RtspChaosServer) -> None:
    mock = HikvisionMock(password=TEST_PASSWORD)
    r = await run_test(make_device("hikvision", kind="nvr", rtsp_port=chaos.port), "Mala#1", transport=asgi(mock.app))
    assert not r.ok and r.auth_ok is False and not r.locked
    assert mock.auth.credentialed == 1                   # una petición con credenciales a la API…
    assert chaos.stats.credentialed == 0                 # …y ninguna al RTSP
    assert "Te quedan 4 intentos" in r.message


async def test_locked_user_is_reported_with_minutes(chaos: RtspChaosServer) -> None:
    mock = HikvisionMock(password=TEST_PASSWORD, locked=True)
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert r.locked and r.lockout_minutes == 30 and r.auth_ok is False
    assert "bloqueado" in r.message and "30 minutos" in r.message
    assert mock.auth.credentialed == 1 and chaos.stats.credentialed == 0


async def test_dahua_lock_message(chaos: RtspChaosServer) -> None:
    mock = DahuaMock(password=TEST_PASSWORD, locked=True)
    r = await run_test(make_device("dahua", kind="nvr", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert r.locked and r.lockout_minutes == 30 and mock.auth.credentialed == 1 and chaos.stats.credentialed == 0


async def test_digest_sha256_api_and_basic_only(chaos: RtspChaosServer) -> None:
    sha = HikvisionMock(password=TEST_PASSWORD, auth_mode="digest-sha256", kind="camera")
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(sha.app))
    assert r.ok and r.info and r.info.kind == "camera"
    both = HikvisionMock(password=TEST_PASSWORD, auth_mode="digest-both", kind="camera")
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(both.app))
    assert r.ok and both.auth.credentialed >= 1

    basic = HikvisionMock(password=TEST_PASSWORD, auth_mode="basic", kind="camera")
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(basic.app))
    assert not r.ok and "Basic" in r.message and basic.auth.credentialed == 0 and r.warnings
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port, allow_basic=True), TEST_PASSWORD,
                       transport=asgi(basic.app))
    assert r.ok


async def test_hikvision_hybrid_dvr_uses_device_channel_ids() -> None:
    mock = HikvisionMock(kind="dvr", password=TEST_PASSWORD, channels=[HikChannel("IP 1"), HikChannel("IP 2")])
    async with RtspChaosServer(password=TEST_PASSWORD, paths={"/Streaming/Channels/101"}) as srv:
        r = await run_test(make_device("hikvision", kind="dvr", rtsp_port=srv.port), TEST_PASSWORD,
                           transport=asgi(mock.app))
    assert r.info and r.info.kind == "dvr"
    assert [c.channel for c in r.channels] == [1, 2, 3, 4, 33, 34]
    assert [c.analog for c in r.channels] == [True] * 4 + [False] * 2
    from vms.core import rtsp
    assert rtsp.preset_paths("hikvision", 33) == ("/Streaming/Channels/3301", "/Streaming/Channels/3302")


async def test_dahua_xvr_ip_channels_after_analog(chaos: RtspChaosServer) -> None:
    mock = DahuaMock(kind="xvr", password=TEST_PASSWORD)
    r = await run_test(make_device("dahua", kind="xvr", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert r.ok and r.info and r.info.kind == "xvr"
    assert [c.channel for c in r.channels] == list(range(1, 9))
    assert [c.analog for c in r.channels] == [True] * 4 + [False] * 4
    # canal IP 7 (3.º IP, «Almacén») sin vídeo; los analógicos no informan
    assert r.channels[6].online is False and r.channels[4].online is True and r.channels[0].online is None


async def test_profile_without_api_tests_rtsp_with_preset(chaos: RtspChaosServer) -> None:
    dev = make_device("uniview", rtsp_port=chaos.port, onvif_port=_closed_port())
    r = await run_test(dev, TEST_PASSWORD)
    assert r.ok and r.rtsp_ok and r.auth_ok
    assert chaos.stats.requests[1] == "DESCRIBE /unicast/c1/s0/live"
    assert chaos.stats.credentialed >= 1 and chaos.stats.rejected == 0


async def test_profile_path_variant_after_404() -> None:
    async with RtspChaosServer(password=TEST_PASSWORD, paths={"/h264/ch1/main/av_stream"}) as srv:
        r = await run_test(make_device("ezviz", rtsp_port=srv.port), TEST_PASSWORD)
        assert r.ok and r.working_path == "/h264/ch1/main/av_stream" and srv.stats.rejected == 0
        bad = await run_test(make_device("ezviz", rtsp_port=srv.port), "Mala#1")
        assert bad.auth_ok is False and srv.stats.rejected == 1   # un solo intento, sin probar las variantes


async def test_firmware_change_with_rtsp_closed() -> None:
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", firmware="V5.7.20")
    dev = make_device("hikvision", rtsp_port=_closed_port(), firmware="V5.7.3")
    r = await run_test(dev, TEST_PASSWORD, transport=asgi(mock.app))
    assert r.firmware_changed == "V5.7.3 → V5.7.20" and r.rtsp_ok is False and not r.ok
    assert "se actualizó" in r.message and "RTSP" in r.message


async def test_smart_codec_sets_gop_hint(chaos: RtspChaosServer) -> None:
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", channels=[HikChannel("Cam", smart_codec=True)])
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert r.ok and r.gop_slow and any("12 s" in w for w in r.warnings)


async def test_sub_h265_suggests_codec_fix(chaos: RtspChaosServer) -> None:
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", channels=[HikChannel("Cam", sub_codec="H.265")])
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert any("Corregir códec" in w for w in r.warnings)


async def test_unknown_vendor_and_unreachable() -> None:
    r = await run_test(DeviceTestRequest.model_construct(name="x", vendor="marca-futura", kind="camera",
                                                         host="127.0.0.1", rtsp_port=554, http_port=80,
                                                         https=False, onvif_port=None, username="", enabled=True,
                                                         notes="", allow_basic=False, follow_ip=False), "")
    assert not r.ok and "no está disponible" in r.message
    port = _closed_port()
    r = await run_test(make_device("reolink", rtsp_port=port), TEST_PASSWORD)
    assert not r.reachable and "no responden" in r.message


async def test_experimental_driver_warns(chaos: RtspChaosServer) -> None:
    r = await run_test(make_device("imou", rtsp_port=chaos.port, onvif_port=_closed_port()), TEST_PASSWORD)
    assert r.ok and any("experimental" in w for w in r.warnings)
