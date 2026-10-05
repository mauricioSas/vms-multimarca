"""B6 con los drivers REALES de B5 contra los simuladores de `tools/mocks` (revisión v2, frente «operación»).

Lo que B6 pide a los drivers tiene que casar con lo que entregan de verdad: marcas sin API (Ezviz, Reolink),
capacidades que se consultan al registro, formas reales del firmware y de la hora.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tests.ops.conftest import Harness, new_device
from tools.mocks.hikvision import HikvisionMock
from vms.core.models import Device
from vms.vendors import client_for

PW = "Cl@ve#1"


def _factory(mock: Any) -> Any:
    """Driver real: Hikvision contra el simulador; el resto, `client_for` sin red (Ezviz/Reolink no tienen API)."""
    def f(dev: Device, password: str) -> Any:
        if dev.vendor == "hikvision":
            return client_for(dev, password, transport=httpx.ASGITransport(app=mock.app))
        return client_for(dev, password)
    return f


# --------------------------------------------------------------------------- A1: marcas sin API
async def test_clock_check_survives_a_brand_without_api(api: Harness) -> None:
    """Una Ezviz en la tienda no tumba la comprobación horaria (antes, 501 y el bucle cortado para todos)."""
    admin = await api.login()
    ez = await new_device(admin, name="Ezviz caja", vendor="ezviz", kind="camera", host="10.0.0.9")
    hik = await new_device(admin, name="Hik pasillo", vendor="hikvision", kind="camera", host="10.0.0.5")
    api.state.client_factory = _factory(HikvisionMock(password=PW, kind="camera", clock_offset_s=120))
    r = await admin.post("/api/clock/check")
    assert r.status_code == 200, r.text
    by = {d["device_id"]: d for d in r.json()["devices"]}
    assert by[ez["id"]]["status"] == "unknown" and "Ezviz" in by[ez["id"]]["message_es"]
    assert by[hik["id"]]["status"] == "critical" and abs(by[hik["id"]]["skew_s"] - 120) < 5


async def test_diagnose_brand_without_api(api: Harness) -> None:
    admin = await api.login()

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.read(1024)
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        d = await new_device(admin, name="Ezviz caja", vendor="ezviz", kind="camera", host="127.0.0.1",
                             http_port=port)
        api.state.client_factory = _factory(HikvisionMock(password=PW, kind="camera"))
        r = await admin.post("/api/diagnostics/device", json={"device_id": d["id"]})
    finally:
        srv.close()
    assert r.status_code == 200, r.text
    auth = next(s for s in r.json()["steps"] if s["code"] == "auth")
    assert auth["ok"] is None and "no tienen API" in auth["detail_es"]


async def test_audit_brand_without_api_keeps_local_findings(api: Harness) -> None:
    """Con credenciales de administrador y una Ezviz: la auditoría sigue (antes se perdía «contraseña de fábrica»)."""
    admin = await api.login()
    d = await new_device(admin, name="Ezviz caja", vendor="ezviz", kind="camera", host="127.0.0.1", password="12345")
    api.state.client_factory = _factory(HikvisionMock(password=PW, kind="camera"))
    ops = api.app.state.ops
    real = ops.security_audit

    async def no_network(creds: Any, user: str, ip: str, deps: Any = None) -> Any:
        from vms.ops.security.audit import AuditDeps

        async def tcp(h: str, p: int, t: float) -> bool:
            return False

        async def rtsp(*a: Any, **k: Any) -> Any:
            class R:
                status = 401
            return R()

        async def onvif(*a: Any) -> bool | None:
            return None
        return await real(creds, user, ip, deps=AuditDeps(client_factory=api.state.client_factory,
                                                          get_password=api.state.creds.get_device_password,
                                                          tcp_check=tcp, rtsp_probe=rtsp, onvif_probe=onvif, ssdp=None))
    ops.security_audit = no_network
    r = await admin.post("/api/security-audit/run",
                         json={"admin_credentials": {d["id"]: {"username": "admin", "password": "Adm#temporal1"}}})
    assert r.status_code == 200, r.text
    by = {f["check"]: f for f in r.json()["findings"]}
    assert by["weak_password"]["status"] == "vulnerable"
    assert by["p2p_cloud"]["status"] == "unknown" and "Ezviz" in by["p2p_cloud"]["detail_es"]


async def test_health_of_brand_without_api_uses_local_rtsp(api: Harness) -> None:
    """Reolink no tiene API: la salud va directa al fotograma del RTSP local (antes, 501 sin llegar al plan B)."""
    admin = await api.login()
    d = await new_device(admin, name="Reolink puerta", vendor="reolink", kind="camera", host="10.0.0.7",
                         import_channels=[1])
    cam = d["cameras"][0]
    calls: list[str] = []

    def read_url(cid: str, stream: str) -> str:
        calls.append(cid)
        return "rtsp://127.0.0.1:1/x"
    api.state.engine.rtsp_read_url = read_url  # type: ignore[method-assign]
    factory_calls: list[str] = []
    real = _factory(HikvisionMock(password=PW, kind="camera"))

    def factory(dev: Device, password: str) -> Any:
        factory_calls.append(dev.vendor)
        return real(dev, password)
    api.state.client_factory = factory
    r = await admin.post(f"/api/camera-health/{cam}/check")
    assert r.status_code == 200, r.text
    assert calls == [cam] and factory_calls == [], "sin API no se pide cliente: directo al RTSP local"
