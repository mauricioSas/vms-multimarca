"""client_for() lanza DeviceUnsupported para Ezviz/Reolink (contrato §5.1 de B5) y B6 lo llama fuera del try."""
from __future__ import annotations

import httpx

from tests.api.conftest import Harness, new_device
from tools.mocks.hikvision import HikvisionMock
from vms.vendors import client_for

PW = "Cl@ve#1"


def _factory(mock: HikvisionMock):
    def f(dev, password):
        if dev.vendor == "hikvision":
            return client_for(dev, password, transport=httpx.ASGITransport(app=mock.app))
        return client_for(dev, password)          # Ezviz -> DeviceUnsupported
    return f


async def test_clock_check_with_ezviz(api: Harness) -> None:
    admin = await api.login()
    await new_device(admin, name="Ezviz caja", vendor="ezviz", kind="camera", host="10.0.0.9")
    await new_device(admin, name="Hik pasillo", vendor="hikvision", kind="camera", host="10.0.0.5")
    api.state.client_factory = _factory(HikvisionMock(password=PW, kind="camera", clock_offset_s=120))
    r = await admin.post("/api/clock/check")
    print("\n[clock] status:", r.status_code, r.text[:300])
    assert r.status_code == 200, "una Ezviz no debería tumbar la comprobación de hora de toda la tienda"


async def test_diagnose_ezviz(api: Harness) -> None:
    admin = await api.login()
    import asyncio
    async def handle(r, w):
        await r.read(1024); w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"); await w.drain(); w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    d = await new_device(admin, name="Ezviz caja", vendor="ezviz", kind="camera", host="127.0.0.1", http_port=port)
    api.state.client_factory = _factory(HikvisionMock(password=PW, kind="camera"))
    r = await admin.post("/api/diagnostics/device", json={"device_id": d["device"]["id"] if "device" in d else d["id"]})
    print("\n[diag] status:", r.status_code, r.text[:300])
    assert r.status_code == 200


async def test_audit_ezviz_with_admin(api: Harness) -> None:
    admin = await api.login()
    d = await new_device(admin, name="Ezviz caja", vendor="ezviz", kind="camera", host="127.0.0.1", password="12345")
    did = d["device"]["id"] if "device" in d else d["id"]
    api.state.client_factory = _factory(HikvisionMock(password=PW, kind="camera"))
    r = await admin.post("/api/security-audit/run",
                         json={"admin_credentials": {did: {"username": "admin", "password": "Adm#temporal1"}}})
    print("\n[audit] status:", r.status_code)
    for f in r.json().get("findings", []):
        print("   ", f["check"], f["status"], "|", f["detail_es"][:80])
    assert any(f["check"] == "weak_password" for f in r.json()["findings"]), "se pierde «contraseña de fábrica»"


async def test_health_reolink_never_reaches_rtsp_fallback(api: Harness) -> None:
    """grab(): sin API debería caer al fotograma RTSP local; con Reolink/Ezviz revienta antes."""
    admin = await api.login()
    d = await new_device(admin, name="Reolink puerta", vendor="reolink", kind="camera", host="10.0.0.7",
                         import_channels=[1])
    cam = d["cameras"][0]
    calls = []
    api.state.engine.rtsp_read_url = lambda cid, stream: calls.append(cid) or "rtsp://127.0.0.1:1/x"
    api.state.client_factory = _factory(HikvisionMock(password=PW, kind="camera"))
    r = await admin.post(f"/api/camera-health/{cam}/check")
    print("\n[health] status:", r.status_code, r.text[:200], "| fallback RTSP llamado:", bool(calls))
    assert r.status_code == 200 and calls
