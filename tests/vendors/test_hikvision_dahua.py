"""Clientes ISAPI (Hikvision) y CGI (Dahua) contra los mocks de tools/mocks (Digest real, sin red)."""
from __future__ import annotations

import httpx
import pytest

from tests.conftest import get_free_port
from tools.mocks.dahua import DahuaChannel, DahuaMock
from tools.mocks.hikvision import HikvisionMock
from vms.core.errors import DeviceAuthFailed, DeviceProtocolError, DeviceUnreachable, DeviceUnsupported
from vms.core.models import Device
from vms.vendors import client_for
from vms.vendors.dahua import DahuaClient, parse_kv
from vms.vendors.hikvision import HikvisionClient


def _dev(vendor: str, kind: str = "nvr", host: str = "10.1.1.1", port: int = 80) -> Device:
    return Device(name="Equipo", vendor=vendor, kind=kind, host=host, http_port=port, username="admin")  # type: ignore[arg-type]


def _asgi(app: object) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Hikvision
async def test_hikvision_nvr_probe_and_channels(hik_mock: HikvisionMock) -> None:
    client = client_for(_dev("hikvision"), hik_mock.password, transport=_asgi(hik_mock.app))
    assert isinstance(client, HikvisionClient)
    try:
        info = await client.probe()
        assert info.vendor == "hikvision" and info.kind == "nvr"
        assert info.model == hik_mock.model and info.serial == hik_mock.serial and info.firmware == "V4.30.085"
        assert info.channel_count == 4
        chans = await client.list_channels()
    finally:
        await client.aclose()
    assert [c.name for c in chans] == ["Entrada", "Cajas", "Almacén", "Pasillo 1"]
    assert [c.online for c in chans] == [True, True, False, True]
    assert chans[0].ip_address == "192.168.254.2"
    assert chans[3].main_codec == "H.265" and chans[3].sub_codec == "H.264"
    assert chans[0].main_resolution == "2560x1440" and chans[0].sub_resolution == "640x360"
    assert all(c.has_sub for c in chans)


async def test_hikvision_snapshot_in_memory(hik_mock: HikvisionMock) -> None:
    client = HikvisionClient(_dev("hikvision"), hik_mock.password, transport=_asgi(hik_mock.app))
    try:
        jpeg = await client.snapshot(1, "sub")
        assert jpeg[:2] == b"\xff\xd8"
        assert "GET /ISAPI/Streaming/channels/102/picture" in hik_mock.requests
        with pytest.raises(DeviceProtocolError):
            await client.snapshot(3, "main")  # canal sin conexión: el NVR responde 500
    finally:
        await client.aclose()


async def test_hikvision_ip_camera() -> None:
    cam = HikvisionMock(kind="camera")
    client = HikvisionClient(_dev("hikvision", "camera"), cam.password, transport=_asgi(cam.app))
    try:
        info = await client.probe()
        chans = await client.list_channels()
    finally:
        await client.aclose()
    assert info.kind == "camera" and info.channel_count == 1 and info.model == "DS-2CD2143G2-I"
    assert len(chans) == 1 and chans[0].channel == 1 and chans[0].main_codec == "H.264"


async def test_hikvision_bad_password_does_not_retry(hik_mock: HikvisionMock) -> None:
    client = HikvisionClient(_dev("hikvision"), "mala", transport=_asgi(hik_mock.app))
    try:
        with pytest.raises(DeviceAuthFailed, match="contraseña incorrectos"):
            await client.probe()
    finally:
        await client.aclose()
    # Digest: una petición sin credenciales + UNA con la contraseña mala; nunca en bucle
    assert len(hik_mock.requests) == 2
    assert hik_mock.auth.failures == 2


async def test_hikvision_not_an_isapi_device() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def other(_: object) -> PlainTextResponse:
        return PlainTextResponse("<html>router</html>")

    app = Starlette(routes=[Route("/ISAPI/System/deviceInfo", other)])
    client = HikvisionClient(_dev("hikvision"), "x", transport=_asgi(app))
    try:
        with pytest.raises(DeviceProtocolError):
            await client.probe()
    finally:
        await client.aclose()


async def test_unreachable_device_maps_to_domain_error() -> None:
    port = get_free_port()  # nadie escucha
    client = HikvisionClient(_dev("hikvision", host="127.0.0.1", port=port), "x", timeout=2)
    try:
        with pytest.raises(DeviceUnreachable, match="No se puede conectar"):
            await client.probe()
    finally:
        await client.aclose()


def test_generic_vendor_has_no_api_client() -> None:
    with pytest.raises(DeviceUnsupported):
        client_for(_dev("generic"), "x")


# --------------------------------------------------------------------------- Dahua
def test_parse_kv_handles_crlf_and_equals_in_values() -> None:
    kv = parse_kv("a=1\r\nb=x=y\r\n\r\nbasura\r\n")
    assert kv == {"a": "1", "b": "x=y"}


async def test_dahua_nvr_probe_and_channels(dahua_mock: DahuaMock) -> None:
    client = client_for(_dev("dahua"), dahua_mock.password, transport=_asgi(dahua_mock.app))
    assert isinstance(client, DahuaClient)
    try:
        info = await client.probe()
        chans = await client.list_channels()
    finally:
        await client.aclose()
    assert info.kind == "nvr" and info.model == "DHI-NVR4208-8P-4KS2/L" and info.serial == dahua_mock.serial
    assert info.channel_count == 4 and info.firmware.startswith("4.001")
    assert [c.channel for c in chans] == [1, 2, 3, 4]
    assert [c.name for c in chans] == ["Entrada", "Cajas", "Almacén", "Pasillo 1"]
    assert [c.online for c in chans] == [True, True, False, True]
    assert chans[3].main_codec == "H.265" and chans[0].sub_codec == "H.264"
    assert chans[0].main_resolution == "2688x1520" and chans[0].sub_resolution == "704x576"


async def test_dahua_camera_and_snapshot() -> None:
    cam = DahuaMock(kind="camera", channels=[DahuaChannel("Puerta")])
    client = DahuaClient(_dev("dahua", "camera"), cam.password, transport=_asgi(cam.app))
    try:
        info = await client.probe()
        chans = await client.list_channels()
        jpeg = await client.snapshot(1)
    finally:
        await client.aclose()
    assert info.kind == "camera" and info.channel_count == 1
    assert len(chans) == 1 and chans[0].name == "Puerta" and chans[0].online is True
    assert jpeg[:2] == b"\xff\xd8"


async def test_dahua_bad_password(dahua_mock: DahuaMock) -> None:
    client = DahuaClient(_dev("dahua"), "mala", transport=_asgi(dahua_mock.app))
    try:
        with pytest.raises(DeviceAuthFailed):
            await client.probe()
    finally:
        await client.aclose()
    assert dahua_mock.auth.failures == 2  # reto + un único intento


async def test_dahua_offline_channel_snapshot_fails(dahua_mock: DahuaMock) -> None:
    client = DahuaClient(_dev("dahua"), dahua_mock.password, transport=_asgi(dahua_mock.app))
    try:
        with pytest.raises(DeviceProtocolError):
            await client.snapshot(3)
    finally:
        await client.aclose()


async def test_clients_over_real_http(hik_mock: HikvisionMock, dahua_mock: DahuaMock, mock_server: object) -> None:
    """Mismo flujo por un puerto TCP real (Digest de httpx contra el servidor uvicorn del mock)."""
    hsrv = mock_server(hik_mock.app)  # type: ignore[operator]
    dsrv = mock_server(dahua_mock.app)  # type: ignore[operator]
    h = client_for(_dev("hikvision", host="127.0.0.1", port=hsrv.port), hik_mock.password)
    d = client_for(_dev("dahua", host="127.0.0.1", port=dsrv.port), dahua_mock.password)
    try:
        assert (await h.probe()).channel_count == 4
        assert len(await d.list_channels()) == 4
    finally:
        await h.aclose()
        await d.aclose()
