"""API de B5 con la app real: GET /api/vendors, rutas por canal, alta con contraseña mala (1 intento),
«Corregir códec» con confirmación y deshacer, identidad y cambio de IP por serie/MAC."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from tests.fakes import FakeEngine
from tests.vendors.conftest import TEST_PASSWORD, asgi
from tools.mocks.hikvision import HikChannel, HikvisionMock
from tools.mocks.rtsp_chaos import RtspChaosServer
from vms.api import create_app
from vms.core.credentials import CredentialStore
from vms.core.interfaces import DiscoveredDevice
from vms.core.models import Device, DeviceBase
from vms.core.settings import VmsSettings
from vms.vendors import client_for, test_device

ADMIN_PW = "Admin#12345"
HEADERS = {"X-Requested-With": "vms"}


@dataclass
class B5Harness:
    app: Any
    mock: HikvisionMock
    chaos: RtspChaosServer
    discovered: list[DiscoveredDevice] = field(default_factory=list)
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def login(self, username: str = "admin", password: str = ADMIN_PW) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 50000)),
                              base_url="http://testserver", headers=HEADERS)
        self.clients.append(c)
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return c

    @property
    def state(self) -> Any:
        return self.app.state.vms


@pytest.fixture
async def b5(settings: VmsSettings, credential_store: CredentialStore) -> AsyncIterator[B5Harness]:
    mock = HikvisionMock(password=TEST_PASSWORD, channels=[HikChannel("Entrada"), HikChannel("Cajas", sub_codec="H.265")])
    holder: dict[str, B5Harness] = {}

    def factory(device: Device, password: str) -> Any:
        return client_for(device, password, transport=asgi(holder["h"].mock.app))

    async def tester(device: DeviceBase, password: str) -> Any:
        return await test_device(device, password, transport=asgi(holder["h"].mock.app))

    async def discoverer(timeout: float) -> list[DiscoveredDevice]:
        return list(holder["h"].discovered)

    s = settings.model_copy(update={"admin_initial_password": SecretStr(ADMIN_PW)})
    app = create_app(s, engine=FakeEngine(), credential_store=credential_store, client_factory=factory,
                     device_tester=tester, discoverer=discoverer, heartbeat=False, apply_delay=0.01)
    async with RtspChaosServer(password=TEST_PASSWORD) as chaos:
        h = B5Harness(app, mock, chaos)
        holder["h"] = h
        async with app.router.lifespan_context(app):
            yield h
            for c in h.clients:
                await c.aclose()


def _device_body(h: B5Harness, **extra: Any) -> dict[str, Any]:
    return {"name": "NVR tienda", "vendor": "hikvision", "kind": "nvr", "host": "127.0.0.1",
            "rtsp_port": h.chaos.port, "http_port": 80, "username": "admin", **extra}


async def test_vendors_listing_is_admin_only_and_has_no_functions(b5: B5Harness) -> None:
    admin = await b5.login()
    r = await admin.get("/api/vendors")
    assert r.status_code == 200
    data = r.json()
    ids = [v["id"] for v in data]
    assert {"hikvision", "dahua", "onvif", "generic", "ezviz", "imou", "uniview", "tplink-vigi", "tapo", "hanwha",
            "axis", "ajax", "reolink", "bosch"} <= set(ids)
    hik = next(v for v in data if v["id"] == "hikvision")
    assert hik["maturity"] in ("verified", "fixtures", "community", "experimental")
    assert hik["preset_examples"][0] == {"channel": 1, "main": "/Streaming/Channels/101",
                                         "sub": "/Streaming/Channels/102", "rtsp_port": 554, "scheme": "rtsp"}
    assert "api_codec_fix" in hik["capabilities"] and hik["lockout"] == {"attempts": 5, "minutes": 30}
    assert next(v for v in data if v["id"] == "generic")["manual_path"] is True
    assert "<function" not in r.text
    r = await admin.get("/api/vendors/dahua/paths", params={"channel": 3, "kind": "nvr"})
    assert r.json()["main"] == "/cam/realmonitor?channel=3&subtype=0" and r.json()["query_safe"] is False
    r = await admin.get("/api/vendors/hikvision/paths", params={"channel": 33, "kind": "dvr"})
    assert r.json()["sub"] == "/Streaming/Channels/3302"
    assert (await admin.get("/api/vendors/ezviz/paths")).json()["variants"] == ["/h264/ch1/main/av_stream",
                                                                                "/Streaming/Channels/101"]
    assert (await admin.get("/api/vendors/nada")).status_code == 404
    # un operador no ve el registro (el alta es de administradores)
    r = await admin.post("/api/users", json={"username": "operador", "password": "Operador#1", "role": "operator"})
    assert r.status_code == 201, r.text
    op = await b5.login("operador", "Operador#1")
    assert (await op.get("/api/vendors")).status_code == 403


async def test_new_vendor_ids_are_accepted_by_the_api(b5: B5Harness) -> None:
    admin = await b5.login()
    r = await admin.post("/api/devices", json={"name": "Cam VIGI", "vendor": "tplink-vigi", "host": "10.0.0.40",
                                               "kind": "camera"})
    assert r.status_code == 201, r.text
    assert (await admin.post("/api/devices", json={"name": "X", "vendor": "marca-futura", "host": "10.0.0.41"})
            ).status_code == 422


async def test_wrong_password_alta_sends_credentials_once(b5: B5Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    admin = await b5.login()
    r = await admin.post("/api/devices/test", json=_device_body(b5, password="Mala#Pass:9@/x"))
    assert r.status_code == 200 and r.json()["auth_ok"] is False
    assert b5.mock.auth.credentialed == 1 and b5.chaos.stats.credentialed == 0
    # guardar con importación de canales: una sola petición con credenciales más (la del alta)
    r = await admin.post("/api/devices", json=_device_body(b5, password="Mala#Pass:9@/x", import_channels="all"))
    assert r.status_code == 201 and "Usuario o contraseña" in r.json()["details"]["import_error"]
    assert b5.mock.auth.credentialed == 2 and b5.chaos.stats.credentialed == 0
    assert "Mala#Pass:9@/x" not in caplog.text


async def test_codec_fix_flow_with_confirmation_and_undo(b5: B5Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vms.audit")
    admin = await b5.login()
    r = await admin.post("/api/devices", json=_device_body(b5, password=TEST_PASSWORD, import_channels="all"))
    dev_id = r.json()["id"]
    r = await admin.post(f"/api/devices/{dev_id}/codec-fix", json={"channel": 2})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["loc"] == ["confirm"]
    assert b5.mock.puts == []
    r = await admin.post(f"/api/devices/{dev_id}/codec-fix", json={"channel": 2, "confirm": True})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["previous_codec"] == "H.265" and b5.mock.channels[1].sub_codec == "H.264"
    backups = b5.state.paths.config_dir / "device-backups" / dev_id
    assert (backups / f"{rec['backup_id']}.xml").is_file()
    hist = (await admin.get(f"/api/devices/{dev_id}/codec-fix")).json()
    assert hist[0]["backup_id"] == rec["backup_id"] and hist[0]["undo_available"] is True
    r = await admin.post(f"/api/devices/{dev_id}/codec-fix/undo", json={"backup_id": rec["backup_id"]})
    assert r.status_code == 200 and b5.mock.channels[1].sub_codec == "H.265"
    events = [json.loads(x.getMessage())["event"] for x in caplog.records if x.name == "vms.audit"]
    assert events == ["codec_fix", "codec_fix_undo"]


async def test_codec_fix_refused_for_profiles(b5: B5Harness) -> None:
    admin = await b5.login()
    r = await admin.post("/api/devices", json={"name": "Uni", "vendor": "uniview", "host": "10.0.0.50"})
    r = await admin.post(f"/api/devices/{r.json()['id']}/codec-fix", json={"channel": 1, "confirm": True})
    assert r.status_code == 422 and "códec" in r.json()["error"]["message"]


async def test_identity_and_ip_change(b5: B5Harness) -> None:
    admin = await b5.login()
    r = await admin.post("/api/devices", json=_device_body(b5, password=TEST_PASSWORD, host="127.0.0.1"))
    dev_id = r.json()["id"]
    r = await admin.post(f"/api/devices/{dev_id}/identity")
    assert r.status_code == 200 and r.json()["serial"] == b5.mock.serial and r.json()["mac"] == b5.mock.mac
    r2 = await admin.post("/api/devices", json={**_device_body(b5), "name": "Cámara 2", "host": "10.0.0.60",
                                                "follow_ip": True, "kind": "camera"})
    dev2 = r2.json()["id"]

    def mutate(cfg: Any) -> None:
        d = cfg.device(dev2)
        data = d.model_dump()
        data["identity"] = {"serial": "", "mac": "AA-BB-CC-00-11-22", "source": "sadp"}
        cfg.devices = [Device.model_validate(data) if x.id == dev2 else x for x in cfg.devices]

    await b5.state.update_config(mutate, "devices")
    b5.discovered = [
        DiscoveredDevice(host="192.168.1.80", serial=b5.mock.serial, sources=["sadp"]),   # misma serie
        DiscoveredDevice(host="10.0.0.61", mac="aa:bb:cc:00:11:22", sources=["wsd"]),     # misma MAC
        DiscoveredDevice(host="10.0.0.99", serial="OTRA-SERIE-123", sources=["dhip"]),
    ]
    assert (await admin.post(f"/api/devices/{dev_id}/move", json={"host": "192.168.1.80"})).status_code == 422
    r = await admin.post("/api/devices/ip-check", json={"timeout_s": 1})
    props = {p["device_id"]: p for p in r.json()["proposals"]}
    assert set(props) == {dev_id, dev2}
    assert props[dev_id]["match"] == "serial" and props[dev_id]["applied"] is False
    assert props[dev2]["match"] == "mac" and props[dev2]["applied"] is True            # follow_ip: sola
    devs = {d["id"]: d for d in (await admin.get("/api/devices")).json()}
    assert devs[dev2]["host"] == "10.0.0.61" and devs[dev_id]["host"] == "127.0.0.1"
    r = await admin.post(f"/api/devices/{dev_id}/move", json={"host": "192.168.1.80"})
    assert r.status_code == 200 and r.json()["host"] == "192.168.1.80"
    devs = {d["id"]: d for d in (await admin.get("/api/devices")).json()}
    assert devs[dev_id]["host"] == "192.168.1.80" and devs[dev_id]["has_password"] is True
    assert b5.state.creds.get_device_password(dev_id) == TEST_PASSWORD     # misma serie: se reutiliza
