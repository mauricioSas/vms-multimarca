"""Auditoría de seguridad (CONTRATO §18.12, criterio 8 de B6).

La tabla valida su esquema; Hikvision se compara por fecha de build y Dahua por versión; el RTSP anónimo se
detecta con un servidor RTSP de prueba (el `rtsp_chaos` de B5 aún no está: `tests/ops/doubles.RtspDouble`);
y nada sale a Internet (todas las conexiones van a 127.0.0.1). Las credenciales de administrador temporales no
se guardan ni se registran.
"""
from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.ops.conftest import Harness, new_device
from tests.ops.doubles import DiagBehavior, DiagClient, RtspDouble
from vms.core.interfaces import DeviceSecuritySettings
from vms.core.models import Device
from vms.ops.models import AdvisoryTable
from vms.ops.security import advisories as adv
from vms.ops.security.audit import AuditDeps, audit_device, password_weakness

PW = "Buena#1234"


def test_version_table_is_valid_and_reviewed() -> None:
    loaded = adv.version_table()
    t = loaded.table
    assert t.schema_ == 1 and t.advisories and loaded.origin == "version"
    assert t.nvd_notice.startswith("This product uses the NVD API")
    ids = [a.id for a in t.advisories]
    assert len(ids) == len(set(ids))
    for a in t.advisories:
        assert a.reviewed and a.cves and a.notes_es
        if a.compare == "build_date" and a.fixed_build_date:
            datetime.fromisoformat(a.fixed_build_date)
        if a.compare == "version":
            assert a.fixed_version and adv.version_tuple(a.fixed_version)


def _write_update(base: Path, generated_at: str, **over: Any) -> None:
    data = json.loads(Path(adv.__file__).with_name("advisories.json").read_text(encoding="utf-8"))
    data.update({"generated_at": generated_at, **over})
    p = adv.update_table_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def test_update_table_wins_only_if_valid_and_newer(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert adv.load_table(tmp_path).origin == "version"
    _write_update(tmp_path, "2027-01-01T00:00:00Z")
    assert adv.load_table(tmp_path).origin == "update"
    _write_update(tmp_path, "2026-10-05T00:00:00Z")       # mismo generated_at → la de la versión
    assert adv.load_table(tmp_path).origin == "version"
    _write_update(tmp_path, "2020-01-01T00:00:00Z")       # más vieja → la de la versión
    assert adv.load_table(tmp_path).origin == "version"
    with caplog.at_level(logging.WARNING):
        _write_update(tmp_path, "2030-01-01T00:00:00Z", schema=2)   # esquema desconocido → se ignora con aviso
        assert adv.load_table(tmp_path).origin == "version"
    assert "no válida" in caplog.text
    adv.update_table_path(tmp_path).write_text("{roto", encoding="utf-8")
    assert adv.load_table(tmp_path).origin == "version"


def test_hikvision_by_build_date_and_dahua_by_version() -> None:
    t = adv.version_table().table
    old = adv.evaluate(t, "hikvision", "DS-2CD2143G2-I", "V5.5.0 build 200101")
    assert any(m.verdict == "probably_vulnerable" and "CVE-2021-36260" in m.advisory.cves for m in old)
    new = adv.evaluate(t, "hikvision", "DS-2CD2143G2-I", "V5.7.3 build 220112")
    assert all(m.verdict == "ok" for m in new if "CVE-2021-36260" in m.advisory.cves)
    via_date = adv.evaluate(t, "hikvision", "DS-2CD2043G0-I", "V5.5.0", firmware_date="2021-06-27")
    assert any(m.verdict == "probably_vulnerable" for m in via_date)
    unknown = adv.evaluate(t, "hikvision", "DS-2CD2043G0-I", "V5.5.0")
    assert all(m.verdict == "unknown" for m in unknown), "sin fecha de build no se inventa un resultado"
    assert adv.evaluate(t, "hikvision", "DS-7608NI-K2/8P", "V4.30.085 build 200916") == [], "NVR: fuera del patrón"
    d_old = adv.evaluate(t, "dahua", "IPC-HFW5231E-Z", "2.800.0000000.8.R.200101")
    assert d_old and d_old[0].verdict == "probably_vulnerable"
    d_new = adv.evaluate(t, "dahua", "IPC-HFW5231E-Z", "2.840.0000000.1.R.220101")
    assert d_new and d_new[0].verdict == "ok"
    assert adv.firmware_build_date("V5.5.800 build 210628") == datetime(2021, 6, 28).date()


def test_password_analysis_is_local() -> None:
    assert password_weakness("12345", "admin") and password_weakness("admin", "admin")
    assert password_weakness("Tienda37", "tienda37")
    assert password_weakness("abcdefgh", "op") and password_weakness("aaaaaaaaaa", "op")
    assert password_weakness("corta1!", "op")
    assert password_weakness("Vms-Lectura-2026!", "vms") is None


class _Ports:
    def __init__(self, open_ports: set[int]) -> None:
        self.open = open_ports
        self.hosts: set[str] = set()

    async def __call__(self, host: str, port: int, timeout: float) -> bool:
        self.hosts.add(host)
        return port in self.open


async def _no_onvif(host: str, port: int, https: bool, timeout: float) -> bool | None:
    return False


async def test_anonymous_rtsp_detected_and_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    rtsp = await RtspDouble(auth="none").start()
    destinations: list[str] = []
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, addr: Any) -> Any:   # nada sale a Internet
        destinations.append(addr[0] if isinstance(addr, tuple) else str(addr))
        return real_connect(self, addr)
    monkeypatch.setattr(socket.socket, "connect", guarded)
    try:
        dev = Device(id="dev-00000001", name="Cámara", vendor="hikvision", kind="camera", host="127.0.0.1",
                     rtsp_port=rtsp.port, username="admin", model="DS-2CD2143G2-I", firmware="V5.5.0 build 200101")
        ports = _Ports({23, 80, 8000})

        async def ssdp(timeout: float) -> set[str]:
            return {"127.0.0.1"}

        deps = AuditDeps(client_factory=lambda d, p: DiagClient(DiagBehavior(), p), get_password=lambda _d: "12345",
                         tcp_check=ports, onvif_probe=_no_onvif, ssdp=ssdp)
        findings = await audit_device(dev, [], deps, adv.version_table(), {"127.0.0.1"}, None, None)
    finally:
        await rtsp.stop()
    by = {f.check: f for f in findings}
    assert by["anonymous_rtsp"].status == "vulnerable" and by["anonymous_rtsp"].severity == "critical"
    assert by["weak_password"].status == "vulnerable" and by["admin_user"].status == "vulnerable"
    assert by["telnet"].status == "vulnerable" and by["http_no_tls"].status == "vulnerable"
    assert by["sdk_port"].status == "vulnerable" and by["upnp"].status == "vulnerable"
    cve = [f for f in findings if f.check == "firmware_cve"]
    assert any(f.status == "probably_vulnerable" and "KEV" in f.detail_es and f.advisory_ids == ["ADV-2026-001"]
               for f in cve), cve
    assert by["p2p_cloud"].status == "unknown", "sin credenciales de administrador no se sabe"
    assert all(f.status != "secure" for f in findings) and not any("segur" in f.detail_es.lower() for f in findings)
    assert set(destinations) <= {"127.0.0.1"} and ports.hosts == {"127.0.0.1"}
    assert rtsp.credentialed == 0, "el acceso anónimo se prueba SIN credenciales"


async def test_admin_credentials_are_used_once_and_never_stored(api: Harness, caplog: pytest.LogCaptureFixture,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    admin = await api.login()
    dev = await new_device(admin, host="127.0.0.1", kind="camera", import_channels=[1])
    b = DiagBehavior(security=DeviceSecuritySettings(telnet_enabled=True, p2p_cloud_enabled=True, upnp_enabled=False))
    ops = api.app.state.ops
    seen: list[tuple[str, str]] = []

    class Client(DiagClient):
        async def security_settings(self, admin_username: str, admin_password: str) -> DeviceSecuritySettings:
            seen.append((admin_username, admin_password))
            return await super().security_settings(admin_username, admin_password)

    real_audit = ops.security_audit

    async def run(creds: Any, user: str, ip: str, deps: Any = None) -> Any:
        return await real_audit(creds, user, ip, deps=AuditDeps(
            client_factory=lambda d, p: Client(b, p), get_password=api.state.creds.get_device_password,
            tcp_check=_Ports(set()), onvif_probe=_no_onvif, ssdp=None,
            rtsp_probe=lambda *a, **k: _status(401)))
    monkeypatch.setattr(ops, "security_audit", run)
    secret = "Admin-Temporal-9!"
    with caplog.at_level(logging.DEBUG):
        r = await admin.post("/api/security-audit/run",
                             json={"admin_credentials": {dev["id"]: {"username": "admin", "password": secret}}})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["admin_credentials_used"] and seen == [("admin", secret)]
    by = {f["check"]: f for f in rep["findings"]}
    assert by["telnet"]["status"] == "vulnerable" and by["p2p_cloud"]["status"] == "vulnerable"
    assert by["upnp"]["status"] == "ok" and by["anonymous_rtsp"]["status"] == "ok"
    assert secret not in caplog.text, "la contraseña temporal no aparece en ningún registro"
    db = api.state.paths.base / "ops" / "ops.sqlite3"
    assert secret.encode() not in db.read_bytes()
    assert (await admin.get("/api/security-audit/latest")).json()["devices_checked"] == 1
    info = (await admin.get("/api/security-audit/advisories")).json()
    assert info["origin"] == "version" and info["advisories"] >= 3
    op = await api.operator()
    assert (await op.post("/api/security-audit/run", json={})).status_code == 403


def _status(code: int) -> Any:
    async def coro() -> Any:
        class R:
            status = code
            reachable = True
        return R()
    return coro()


def test_table_rejects_bad_regex() -> None:
    bad = adv._parse(json.dumps({"schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "advisories": [
        {"id": "ADV-2026-999", "vendor": "x", "model_regex": "([", "compare": "version", "cves": ["CVE-1"],
         "reviewed": "2026-10-05"}]}), "prueba")
    assert bad is None
    assert AdvisoryTable.model_json_schema()["properties"]["schema"]
