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


# --------------------------------------------------------------------------- A2/A3: firmware y marca reales
async def _firmware_findings(dev: Device) -> list[Any]:
    from vms.ops.security.advisories import version_table
    from vms.ops.security.audit import AuditDeps, audit_device

    async def tcp(h: str, p: int, t: float) -> bool:
        return False

    async def rtsp(*a: Any, **k: Any) -> Any:
        class R:
            status = 401
        return R()

    async def onvif(*a: Any) -> bool | None:
        return False
    deps = AuditDeps(client_factory=lambda d, p: None, get_password=lambda i: "Larga#Clave99",  # type: ignore[arg-type,return-value]
                     tcp_check=tcp, rtsp_probe=rtsp, onvif_probe=onvif, ssdp=None)
    return [f for f in await audit_device(dev, [], deps, version_table(), None, None, None)
            if f.check == "firmware_cve"]


async def test_hikvision_build_date_is_saved_and_audited(api: Harness) -> None:
    """Driver ISAPI real → alta → auditoría: la fecha del build (que Hikvision da aparte) se guarda y la auditoría
    detecta CVE-2021-36260 (KEV). Antes se perdía al guardar y salía «Desconocido»."""
    mock = HikvisionMock(password=PW, kind="camera", firmware="V5.5.0", firmware_released="build 200101")
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    d = await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5",
                         password=PW, import_channels=[1])
    saved = api.state.config().device(d["id"])
    assert saved is not None and (saved.firmware, saved.firmware_date) == ("V5.5.0", "2020-01-01")
    found = await _firmware_findings(saved)
    assert any(f.status == "probably_vulnerable" and f.advisory_ids == ["ADV-2026-001"] for f in found), found

    # leer la identidad de nuevo (equipo actualizado): firmware y fecha cambian juntos
    mock.firmware, mock.firmware_released = "V5.7.3", "build 220112"
    r = await admin.post(f"/api/devices/{d['id']}/identity")
    assert r.status_code == 200, r.text
    saved = api.state.config().device(d["id"])
    assert saved is not None and (saved.firmware, saved.firmware_date) == ("V5.7.3", "2022-01-12")


async def test_probar_conexion_refreshes_firmware(api: Harness) -> None:
    """«Probar conexión» de un equipo guardado deja al día firmware y fecha (lo que pide la auditoría)."""
    from vms.core.interfaces import DeviceInfo, DeviceTestResult
    admin = await api.login()
    d = await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5")

    async def tester(device: Any, password: str) -> DeviceTestResult:
        return DeviceTestResult(ok=True, reachable=True, auth_ok=True, info=DeviceInfo(
            vendor="hikvision", kind="camera", model="DS-2CD2143G2-I", firmware="V5.5.0", firmware_date="2020-01-01"))
    api.state.device_tester = tester
    assert (await admin.post(f"/api/devices/{d['id']}/test")).status_code == 200
    saved = api.state.config().device(d["id"])
    assert saved is not None and (saved.model, saved.firmware, saved.firmware_date) == \
        ("DS-2CD2143G2-I", "V5.5.0", "2020-01-01")


async def test_hikvision_registered_as_onvif_is_not_a_false_ok(api: Harness) -> None:
    """Hikvision dada de alta como «ONVIF (otras marcas)» con firmware vulnerable: antes «Sin CVE conocidos»."""
    from tools.mocks.onvif import OnvifMock
    mock = OnvifMock(password=PW, firmware="V5.5.0 build 200101")      # HIKVISION DS-2CD2143G2-I por ONVIF
    api.state.client_factory = lambda dev, pw: client_for(dev, pw, transport=httpx.ASGITransport(app=mock.app))
    admin = await api.login()
    d = await new_device(admin, name="Cámara ONVIF", vendor="onvif", kind="camera", host="10.0.0.6",
                         password=PW, import_channels=[1])
    saved = api.state.config().device(d["id"])
    assert saved is not None and saved.manufacturer == "HIKVISION"
    found = await _firmware_findings(saved)
    assert any(f.status == "probably_vulnerable" and f.advisory_ids == ["ADV-2026-001"] for f in found), found


async def test_unknown_brand_or_uncovered_brand_is_unknown_never_ok() -> None:
    base = dict(name="x", kind="camera", host="10.0.0.8", model="X-1000", firmware="1.2.3")
    for dev in (Device(vendor="onvif", manufacturer="ACME Vision", **base),     # marca que nadie conoce
                Device(vendor="onvif", **base),                                 # ONVIF sin fabricante
                Device(vendor="generic", **base),
                Device(vendor="axis", **base),                                  # marca sin avisos en la tabla
                Device(vendor="onvif", manufacturer="AXIS", **base)):
        found = await _firmware_findings(dev)
        assert [f.status for f in found] == ["unknown"], (dev.vendor, dev.manufacturer, found)
    # marca cubierta por la tabla y modelo fuera de los avisos: aquí sí cabe «Sin CVE conocidos en la tabla»
    nvr = Device(vendor="hikvision", **{**base, "model": "DS-7608NI-K2/8P", "firmware": "V4.30.085",
                                        "firmware_date": "2020-09-16"})
    assert [f.status for f in await _firmware_findings(nvr)] == ["ok"]
