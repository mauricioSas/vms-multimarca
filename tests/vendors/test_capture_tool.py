"""Herramienta de captura: solo lecturas, contraseña por getpass sin guardarla, anonimización y que lo capturado
se reproduce con la batería (captura de un «equipo» servido en un puerto real)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.vendors.conftest import TEST_PASSWORD
from tools.mocks.hikvision import HikvisionMock
from tools.mocks.replay import ReplayTransport
from tools.mocks.rtsp_chaos import RtspChaosServer
from tools.mocks.server import MockHttpServer
from vms.core.interfaces import DetectionHints
from vms.core.models import DeviceBase
from vms.vendors import client_for
from vms.vendors.capture import Anonymizer, CaptureRefused, RecordingTransport, capture, main


async def test_capture_over_the_network_is_anonymized_and_replayable(tmp_path: Path) -> None:
    mock = HikvisionMock(password=TEST_PASSWORD)
    async with RtspChaosServer(password=TEST_PASSWORD) as chaos:
        with MockHttpServer(mock.app) as srv:
            dev = DeviceBase(name="NVR de la tienda 37", vendor="hikvision", kind="nvr", host="127.0.0.1",
                             http_port=srv.port, rtsp_port=chaos.port, username="admin")
            folder = await capture(dev, TEST_PASSWORD, tmp_path, hints=DetectionHints(sadp=True), source="prueba")
    files = list(folder.rglob("*"))
    text = "\n".join(p.read_text(encoding="utf-8") for p in files if p.is_file())
    for secret in (TEST_PASSWORD, mock.serial, mock.mac, "Entrada", "Almacén", "Network Video Recorder",
                   "NVR de la tienda 37", '"authorization"', "Digest username", "response="):
        assert secret not in text, secret
    assert "nonce-anon-1" in text and "hikvision-realm" in text
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["channels"] == 4 and meta["main_codec"] == "H.264" and meta["synthetic"] is False
    assert not any(p.suffix in (".jpg", ".jpeg") for p in files)         # RGPD: nunca imágenes
    assert all(e not in mock.requests for e in ("PUT", "DELETE"))
    assert all(r.startswith("GET ") for r in mock.requests)
    # lo capturado se reproduce
    client = client_for(DeviceBase(name="r", vendor="hikvision", kind="nvr", host="127.0.0.1", username="admin"),
                        TEST_PASSWORD, transport=ReplayTransport(folder, TEST_PASSWORD))
    try:
        assert len(await client.list_channels()) == 4
    finally:
        await client.aclose()


class _Echo(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="OK")


@pytest.mark.parametrize(("method", "url", "content"), [
    ("PUT", "http://cam/ISAPI/Streaming/channels/102", b"<x/>"),
    ("DELETE", "http://cam/ISAPI/x", b""),
    ("GET", "http://cam/cgi-bin/configManager.cgi?action=setConfig&a=b", b""),
    ("GET", "http://cam/cgi-bin/magicBox.cgi?action=reboot", b""),
    ("POST", "http://cam/onvif/device_service", b"<s:Envelope><s:Body><tds:SystemReboot/></s:Body></s:Envelope>"),
    ("POST", "http://cam/onvif/media_service", b"<s:Envelope><s:Body><trt:SetVideoEncoderConfiguration/></s:Body></s:Envelope>"),
])
async def test_recording_transport_only_reads(method: str, url: str, content: bytes) -> None:
    rec = RecordingTransport(_Echo())
    async with httpx.AsyncClient(transport=rec) as c:
        with pytest.raises(CaptureRefused):
            await c.request(method, url, content=content or None)
    assert rec.exchanges == []


async def test_recording_transport_allows_soap_get_and_hides_images() -> None:
    class Img(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0 foto", headers={"content-type": "image/jpeg"})
    rec = RecordingTransport(Img())
    async with httpx.AsyncClient(transport=rec) as c:
        await c.post("http://cam/onvif/device_service", content=b"<s:Envelope><s:Body><tds:GetDeviceInformation/>"
                                                                b"</s:Body></s:Envelope>")
        await c.get("http://cam/ISAPI/Streaming/channels/101/picture")
    assert [e.soap_op for e in rec.exchanges] == ["GetDeviceInformation", ""]
    assert all("foto" not in e.body and e.binary for e in rec.exchanges)


def test_anonymizer_is_deterministic() -> None:
    a, b = Anonymizer(), Anonymizer()
    for an in (a, b):
        an.add_serial("DS-2CD2143G2-I20210315AAWRG12345678", "DS-2CD2143G2-I")
    text = "sn DS-2CD2143G2-I20210315AAWRG12345678 ip 192.168.1.64 gw 192.168.1.1 mask 255.255.255.0 mac C4-2F-90-F1-E6-A1"
    assert a.text(text) == b.text(text)
    out = a.text(text)
    assert "AAWRG" not in out and "192.168.1.64" not in out and "255.255.255.0" in out and "C4-2F" not in out
    assert out.startswith("sn DS-2CD2143G2-ISN")                     # conserva el modelo, no la serie
    assert a.challenge('Digest realm="IP Camera(C1234)", nonce="abc", opaque="x"', "hikvision") == \
        'Digest realm="hikvision-realm", nonce="nonce-anon-1", opaque="opaque-anon-2"'


def test_cli_asks_password_with_getpass_and_never_prints_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    asked: list[str] = []
    seen: dict[str, Any] = {}

    def fake_getpass(prompt: str) -> str:
        asked.append(prompt)
        return TEST_PASSWORD

    async def fake_capture(device: DeviceBase, password: str, out: Path, **kw: Any) -> Path:
        seen.update(device=device, password=password, out=out)
        return out / "X__Y"

    monkeypatch.setattr("vms.vendors.capture.getpass.getpass", fake_getpass)
    monkeypatch.setattr("vms.vendors.capture.capture", fake_capture)
    rc = main(["--host", "192.168.1.64", "--driver", "tapo", "--user", "visor", "--out", str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 0 and asked and "visor@192.168.1.64" in asked[0]
    assert seen["password"] == TEST_PASSWORD and seen["device"].rtsp_port == 554 and seen["device"].vendor == "tapo"
    assert TEST_PASSWORD not in out.out + out.err
