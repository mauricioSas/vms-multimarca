"""Los mocks deben comportarse como los equipos reales (Digest, formatos, errores)."""
from __future__ import annotations

import hashlib
import socket
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable

import httpx
import pytest

from tools.mocks.dahua import DahuaMock
from tools.mocks.hikvision import HikvisionMock
from tools.mocks.onvif import OnvifMock, hikvision_scopes
from tools.mocks.wsdiscovery import ProbeTarget, WsDiscoveryResponder

HIK_NS = {"h": "http://www.hikvision.com/ver20/XMLSchema"}


async def test_hikvision_isapi_with_digest(hik_mock: HikvisionMock, asgi_client: Callable[..., Any]) -> None:
    client = asgi_client(hik_mock.app, auth=httpx.DigestAuth(hik_mock.username, hik_mock.password))
    r = await client.get("/ISAPI/System/deviceInfo")
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    assert root.findtext("h:deviceType", namespaces=HIK_NS) == "NVR"
    assert root.findtext("h:model", namespaces=HIK_NS) == hik_mock.model

    r = await client.get("/ISAPI/ContentMgmt/InputProxy/channels")
    chans = ET.fromstring(r.text).findall("h:InputProxyChannel", HIK_NS)
    assert [c.findtext("h:name", namespaces=HIK_NS) for c in chans] == ["Entrada", "Cajas", "Almacén", "Pasillo 1"]

    r = await client.get("/ISAPI/ContentMgmt/InputProxy/channels/status")
    online = [s.findtext("h:online", namespaces=HIK_NS) for s in ET.fromstring(r.text).findall("h:InputProxyChannelStatus", HIK_NS)]
    assert online == ["true", "true", "false", "true"]

    r = await client.get("/ISAPI/Streaming/channels/402")
    assert ET.fromstring(r.text).findtext("h:Video/h:videoCodecType", namespaces=HIK_NS) == "H.264"
    r = await client.get("/ISAPI/Streaming/channels/401")
    assert ET.fromstring(r.text).findtext("h:Video/h:videoCodecType", namespaces=HIK_NS) == "H.265"

    r = await client.get("/ISAPI/Streaming/channels/101/picture")
    assert r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert (await client.get("/ISAPI/Streaming/channels/301/picture")).status_code == 500
    r = await client.get("/ISAPI/Nada")
    assert r.status_code == 404 and "notSupport" in r.text


async def test_hikvision_rejects_bad_password(hik_mock: HikvisionMock, asgi_client: Callable[..., Any]) -> None:
    client = asgi_client(hik_mock.app, auth=httpx.DigestAuth("admin", "mala"))
    r = await client.get("/ISAPI/System/deviceInfo")
    assert r.status_code == 401 and "Digest" in r.headers["www-authenticate"]
    anon = asgi_client(hik_mock.app)
    assert (await anon.get("/ISAPI/System/deviceInfo")).status_code == 401


async def test_hikvision_ip_camera_has_no_input_proxy(asgi_client: Callable[..., Any]) -> None:
    cam = HikvisionMock(kind="camera")
    client = asgi_client(cam.app, auth=httpx.DigestAuth(cam.username, cam.password))
    r = await client.get("/ISAPI/System/deviceInfo")
    assert "<deviceType>IPCamera</deviceType>" in r.text
    assert (await client.get("/ISAPI/ContentMgmt/InputProxy/channels")).status_code == 404


async def test_dahua_cgi(dahua_mock: DahuaMock, asgi_client: Callable[..., Any]) -> None:
    client = asgi_client(dahua_mock.app, auth=httpx.DigestAuth(dahua_mock.username, dahua_mock.password))
    r = await client.get("/cgi-bin/magicBox.cgi", params={"action": "getDeviceType"})
    assert r.text == "type=DHI-NVR4208-8P-4KS2/L\r\n"
    r = await client.get("/cgi-bin/configManager.cgi", params={"action": "getConfig", "name": "ChannelTitle"})
    assert "table.ChannelTitle[1].Name=Cajas\r\n" in r.text
    r = await client.get("/cgi-bin/configManager.cgi", params={"action": "getConfig", "name": "Encode"})
    assert "table.Encode[3].MainFormat[0].Video.Compression=H.265" in r.text
    r = await client.get("/cgi-bin/LogicDeviceManager.cgi", params={"action": "getCameraState", "uniqueChannels[0]": "-1"})
    assert "states[2].connectionState=Unconnect" in r.text
    r = await client.get("/cgi-bin/snapshot.cgi", params={"channel": "1"})
    assert r.content[:2] == b"\xff\xd8"
    r = await client.get("/cgi-bin/magicBox.cgi", params={"action": "inventada"})
    assert r.status_code == 400 and "Bad Request" in r.text
    bad = asgi_client(dahua_mock.app, auth=httpx.DigestAuth("admin", "mala"))
    assert (await bad.get("/cgi-bin/magicBox.cgi", params={"action": "getDeviceType"})).status_code == 401


async def test_dahua_rpc2_login_challenge(dahua_mock: DahuaMock, asgi_client: Callable[..., Any]) -> None:
    client = asgi_client(dahua_mock.app)

    def md5u(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest().upper()

    first = (await client.post("/RPC2_Login", json={"method": "global.login", "id": 1, "params": {
        "userName": "admin", "password": "", "clientType": "Web3.0"}})).json()
    assert first["result"] is False and first["error"]["code"] == 268632079
    realm, random, session = first["params"]["realm"], first["params"]["random"], first["session"]
    pw = md5u(f"admin:{random}:{md5u(f'admin:{realm}:{dahua_mock.password}')}")
    second = (await client.post("/RPC2_Login", json={"method": "global.login", "id": 2, "session": session, "params": {
        "userName": "admin", "password": pw, "clientType": "Web3.0", "authorityType": "Default"}})).json()
    assert second["result"] is True
    r = (await client.post("/RPC2", json={"method": "configManager.getConfig", "id": 3, "session": session,
                                          "params": {"name": "ChannelTitle"}})).json()
    assert [t["Name"] for t in r["params"]["table"]][:2] == ["Entrada", "Cajas"]
    r = (await client.post("/RPC2", json={"method": "magicBox.getDeviceType", "id": 4, "session": "falsa"})).json()
    assert r["result"] is False


async def test_onvif_mock_with_real_client(mock_server: Callable[[Any], Any]) -> None:
    from onvif import ONVIFCamera

    mock = OnvifMock(rtsp_base="rtsp://127.0.0.1:1554")
    srv = mock_server(mock.app)
    mock.base_url = srv.base_url
    cam = ONVIFCamera("127.0.0.1", srv.port, mock.username, mock.password, adjust_time=True)
    try:
        await cam.update_xaddrs()
        info = await (await cam.create_devicemgmt_service()).GetDeviceInformation()
        assert info.Manufacturer == "HIKVISION"
        media = await cam.create_media_service()
        profiles = await media.GetProfiles()
        assert [p.token for p in profiles] == ["Profile_1", "Profile_2"]
        uri = await media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                                        "ProfileToken": "Profile_2"})
        assert uri.Uri == "rtsp://127.0.0.1:1554/Streaming/Channels/102"
    finally:
        await cam.close()
    bad = ONVIFCamera("127.0.0.1", srv.port, mock.username, "mala")
    try:
        with pytest.raises(Exception, match="not Authorized"):
            await bad.update_xaddrs()
            await (await bad.create_devicemgmt_service()).GetDeviceInformation()
    finally:
        await bad.close()


def test_ws_discovery_responder_answers_probe() -> None:
    addr_uuid = str(uuid.uuid4())
    target = ProbeTarget(addr_uuid, "http://192.168.1.64/onvif/device_service", hikvision_scopes())
    probe_id = f"uuid:{uuid.uuid4()}"
    probe = ('<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
             'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
             'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
             'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header>'
             f"<w:MessageID>{probe_id}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
             "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header><e:Body>"
             "<d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>")
    with WsDiscoveryResponder([target]) as responder, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(2)
        s.sendto(probe.encode(), ("127.0.0.1", responder.port))
        data, _ = s.recvfrom(65535)
    text = data.decode()
    assert f"<wsa:RelatesTo>{probe_id}</wsa:RelatesTo>" in text
    assert addr_uuid in text and "192.168.1.64/onvif/device_service" in text
    assert "onvif://www.onvif.org/hardware/DS-2CD2143G2-I" in text
    ET.fromstring(text)  # XML bien formado
