"""ONVIF (cliente real onvif-zeep-async contra el mock) y WS-Discovery propio."""
from __future__ import annotations

import uuid
from typing import Any, Callable

import pytest

from tools.mocks.onvif import OnvifMock, OnvifProfile, dahua_scopes, hikvision_scopes, probe_match_xml
from tools.mocks.wsdiscovery import ProbeTarget, WsDiscoveryResponder
from vms.core.errors import DeviceAuthFailed
from vms.core.models import Device
from vms.vendors import client_for, discover, guess_vendor, parse_probe_matches, rtsp_path_of
from vms.vendors.discovery import build_probe
from vms.vendors.onvif_client import OnvifClient


def _onvif_device(port: int) -> Device:
    return Device(name="Cámara ONVIF", vendor="onvif", host="127.0.0.1", http_port=port, username="admin")


def test_rtsp_path_of_strips_host_and_credentials() -> None:
    assert rtsp_path_of("rtsp://u:p@10.0.0.2:554/Streaming/Channels/101") == "/Streaming/Channels/101"
    assert rtsp_path_of("rtsp://10.0.0.2/cam/realmonitor?channel=1&subtype=1") == "/cam/realmonitor?channel=1&subtype=1"


async def test_onvif_probe_channels_and_snapshot(mock_server: Callable[[Any], Any]) -> None:
    mock = OnvifMock(rtsp_base="rtsp://127.0.0.1:554")
    srv = mock_server(mock.app)
    mock.base_url = srv.base_url
    client = client_for(_onvif_device(srv.port), mock.password)
    assert isinstance(client, OnvifClient)
    try:
        info = await client.probe()
        chans = await client.list_channels()
        jpeg = await client.snapshot(1, "sub")
    finally:
        await client.aclose()
    assert info.vendor == "onvif" and info.kind == "camera" and info.name == "HIKVISION"
    assert info.model == "DS-2CD2143G2-I" and info.channel_count == 1
    assert len(chans) == 1
    ch = chans[0]
    assert ch.main_path == "/Streaming/Channels/101" and ch.sub_path == "/Streaming/Channels/102"
    assert ch.main_codec == "H.264" and ch.main_resolution == "2560x1440" and ch.sub_resolution == "640x360"
    assert jpeg[:2] == b"\xff\xd8"


async def test_onvif_orders_profiles_by_resolution(mock_server: Callable[[Any], Any]) -> None:
    # H.265 solo se describe bien por Media2 (Profile T); el mock imita a un equipo con Media2
    mock = OnvifMock(profiles=[OnvifProfile("p-low", "low", 640, 360, "/low"),
                               OnvifProfile("p-high", "high", 1920, 1080, "/high", encoding="H265")], media2=True)
    srv = mock_server(mock.app)
    mock.base_url = srv.base_url
    client = OnvifClient(_onvif_device(srv.port), mock.password)
    try:
        ch = (await client.list_channels())[0]
    finally:
        await client.aclose()
    assert ch.main_path == "/high" and ch.main_codec == "H.265" and ch.sub_path == "/low"


async def test_onvif_bad_password(mock_server: Callable[[Any], Any]) -> None:
    mock = OnvifMock()
    srv = mock_server(mock.app)
    mock.base_url = srv.base_url
    client = OnvifClient(_onvif_device(srv.port), "mala")
    try:
        with pytest.raises(DeviceAuthFailed):
            await client.probe()
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- WS-Discovery
def test_guess_vendor() -> None:
    assert guess_vendor(hikvision_scopes()) == "hikvision"
    assert guess_vendor(dahua_scopes()) == "dahua"
    # v2: Axis tiene perfil propio; una marca sin driver cae en ONVIF genérico
    assert guess_vendor(["onvif://www.onvif.org/name/AXIS"], "P3245") == "axis"
    assert guess_vendor(["onvif://www.onvif.org/name/VIVOTEK"], "FD9389") == "onvif"
    # «IPC-» ya no implica Dahua: el modelo de Uniview gana con su prefijo y el nombre manda sobre el modelo
    assert guess_vendor([], "IPC2122LB-SF28-A") == "uniview"
    assert guess_vendor(["onvif://www.onvif.org/name/UNV"], "IPC-HDW2431T") == "uniview"


def test_parse_probe_matches_filters_other_probes() -> None:
    xml = probe_match_xml("uuid:otro", address_uuid=str(uuid.uuid4()), xaddr="http://10.0.0.9/onvif/device_service",
                          scopes=hikvision_scopes()).encode()
    assert parse_probe_matches(xml, "uuid:mio") == []
    found = parse_probe_matches(xml, "uuid:otro")
    assert len(found) == 1 and found[0].host == "10.0.0.9" and found[0].http_port == 80
    assert found[0].vendor_guess == "hikvision" and found[0].model == "DS-2CD2143G2-I"
    assert found[0].mac == "c4:2f:90:f1:e6:a1"
    assert parse_probe_matches(b"no es xml") == []


def test_probe_message_is_valid_xml() -> None:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(build_probe("uuid:1234"))
    assert any(el.text == "dn:NetworkVideoTransmitter" for el in root.iter())


async def test_discover_with_unicast_targets() -> None:
    targets = [
        ProbeTarget(str(uuid.uuid4()), "http://192.168.1.64:8080/onvif/device_service", hikvision_scopes()),
        ProbeTarget(str(uuid.uuid4()), "http://192.168.1.108/onvif/device_service", dahua_scopes()),
    ]
    with WsDiscoveryResponder(targets) as responder:
        found = await discover(timeout=1.0, targets=[("127.0.0.1", responder.port)])
        assert responder.probes_received >= 1
    by_host = {d.host: d for d in found}
    assert set(by_host) == {"192.168.1.64", "192.168.1.108"}
    assert by_host["192.168.1.64"].http_port == 8080 and by_host["192.168.1.64"].vendor_guess == "hikvision"
    assert by_host["192.168.1.108"].vendor_guess == "dahua"


async def test_discover_returns_empty_when_nobody_answers() -> None:
    from tests.conftest import get_free_port
    assert await discover(timeout=0.3, targets=[("127.0.0.1", get_free_port())]) == []
