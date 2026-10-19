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
from tools.mocks.onvif import OnvifMock
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
    assert r.message.startswith("Grabador DS-7608NI-K2/8P encontrado con 4 canales.")
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


async def test_user_camera_with_rfc2069_rtsp_challenge_connects() -> None:
    """8/10, cámara del usuario (Hikvision DS-2CD1741FWD-IZ, firmware V5.5.0), con la contraseña buena: «Cámara
    DS-2CD1741FWD-IZ encontrado. Prueba RTSP del canal 1: Usuario o contraseña RTSP incorrectos.» Su RTSP reta sin
    qop ni algorithm y daba 401 a nuestra respuesta, que llevaba «algorithm=MD5»."""
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", model="DS-2CD1741FWD-IZ", firmware="V5.5.0")
    async with RtspChaosServer(password=TEST_PASSWORD, scenario="digest-no-algorithm") as srv:
        r = await run_test(make_device("hikvision", rtsp_port=srv.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert r.ok and r.auth_ok is True and r.rtsp_ok is True, r.message
    assert "Vídeo RTSP correcto en el canal 1" in r.message and "incorrect" not in r.message
    assert srv.stats.rejected == 0 and mock.auth.rejected_credentials == 0


async def test_found_header_agrees_with_the_kind(chaos: RtspChaosServer) -> None:
    """El mismo 8/10 la cabecera decía «Cámara DS-2CD1741FWD-IZ encontrado»: «cámara» es femenino."""
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", model="DS-2CD1741FWD-IZ")
    r = await run_test(make_device("hikvision", rtsp_port=chaos.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert r.ok and r.message.startswith("Cámara DS-2CD1741FWD-IZ encontrada."), r.message


async def test_rtsp_rejecting_what_the_api_accepted_does_not_blame_the_password() -> None:
    """Si la API acaba de aceptar el usuario y la contraseña y el RTSP los rechaza, la contraseña es buena: decía
    «Usuario o contraseña RTSP incorrectos» y ponía «Usuario y contraseña» en rojo, así que el usuario la cambiaba y
    volvía a probar, gastando intentos del bloqueo. Ahora dice que es el vídeo el que la rechaza y qué hacer."""
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera")
    async with RtspChaosServer(password="Otra#Pass:2") as srv:     # el RTSP no acepta la misma contraseña
        r = await run_test(make_device("hikvision", rtsp_port=srv.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert not r.ok and r.auth_ok is True and r.rtsp_ok is False and not r.locked
    assert "incorrect" not in r.message and "Qué hacer:" in r.message
    assert "acepta el usuario y la contraseña, pero el vídeo (RTSP) del canal 1 los rechaza" in r.message
    assert "no cambies la contraseña" in r.message and "permiso para ver el vídeo en directo" in r.message
    assert "una sola vez" in r.message                                  # igual que «¿Por qué no conecta?»
    assert srv.stats.credentialed == 1 and srv.stats.rejected == 1     # un solo intento por RTSP


@pytest.mark.parametrize("vendor", ["onvif", "uniview"])
async def test_onvif_answering_any_password_does_not_vouch_for_rtsp(vendor: str) -> None:
    """Muchas cámaras ONVIF genéricas tienen la autenticación ONVIF desactivada: contestan con cualquier
    contraseña. Que ONVIF «acepte» una contraseña MALA no dice nada del RTSP, que la rechaza: «Usuario y
    contraseña» tiene que seguir en rojo (`auth_ok` False, así la web guarda el equipo sin cámaras y el motor no
    manda esa contraseña por RTSP) y el mensaje no puede decir que es correcta ni que no se cambie."""
    async with RtspChaosServer(password="Otra#Pass:2") as srv:     # la contraseña de verdad es otra
        onv = OnvifMock(password=TEST_PASSWORD, auth_disabled=True, rtsp_base=f"rtsp://127.0.0.1:{srv.port}")
        r = await run_test(make_device(vendor, rtsp_port=srv.port), "Mala#1", transport=asgi(onv.app))
    assert onv.credentialed >= 1 and onv.rejected == 0 and r.info is not None   # ONVIF contestó a todo
    assert not r.ok and r.auth_ok is False and r.rtsp_ok is False and not r.locked, r.message
    assert "es correcta" not in r.message and "no cambies la contraseña" not in r.message
    assert "rechaza el usuario o la contraseña" in r.message and "Qué hacer:" in r.message
    assert srv.stats.credentialed == 1 and srv.stats.rejected == 1     # un solo intento por RTSP


async def test_rtsp_rejection_without_api_says_what_to_do() -> None:
    """Sin API, el RTSP es la única prueba de la contraseña: su rechazo sí es «usuario o contraseña» y ahora lleva
    «Qué hacer:» (antes solo «Prueba RTSP del canal 1: Usuario o contraseña RTSP incorrectos.»)."""
    async with RtspChaosServer(password=TEST_PASSWORD) as srv:
        r = await run_test(make_device("reolink", rtsp_port=srv.port), "Mala#1")
    assert not r.ok and r.auth_ok is False and "rechaza el usuario o la contraseña" in r.message
    assert "Qué hacer:" in r.message and "una sola vez" in r.message and srv.stats.credentialed == 1


async def test_rtsp_lock_after_api_accepted_still_marks_the_user_locked() -> None:
    """Un bloqueo que dice el RTSP sí cuenta aunque la API haya aceptado: el alta no debe volver a mandar nada."""
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera")
    async with RtspChaosServer(password=TEST_PASSWORD, scenario="lockout-after-3", lock_after=1) as srv:
        srv.locked = True
        r = await run_test(make_device("hikvision", rtsp_port=srv.port), TEST_PASSWORD, transport=asgi(mock.app))
    assert not r.ok and r.locked and r.auth_ok is False and r.lockout_minutes == 30
    assert "bloqueado" in r.message and srv.stats.credentialed == 1


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
