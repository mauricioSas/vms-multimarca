"""POST /api/discovery/scan explica una búsqueda vacía (CONTRATO §6.3, campo `report`).

El caso del PC del usuario (8/10/2026): «Buscar en la red» no encontraba nada y no decía por qué. Las dos tarjetas
estaban en «Pública» (el cable de las cámaras era una «Red no identificada») y SADPServer.exe de HikCentral tenía
el puerto 37020. Sin red: `discover()` real con sus búsquedas sustituidas por dobles.
"""
from __future__ import annotations

import logging

import pytest

from tests.api.conftest import Harness
from vms.core.interfaces import DiscoveredDevice
from vms.vendors import dhip, discovery, localnet, sadp, scanreport, winnet
from vms.vendors.scanreport import ListenStatus
from vms.vendors.winnet import PortHolder, parse_profiles

# El nombre de la wifi es inventado: el de verdad identifica la casa del usuario y el repositorio es público.
PROFILES_JSON = ('[{"InterfaceAlias":"Wi-Fi","InterfaceIndex":8,"NetworkCategory":0,"IPv4Connectivity":4,'
                 '"Name":"MiWifi-Casa"},{"InterfaceAlias":"Ethernet","InterfaceIndex":18,'
                 '"NetworkCategory":0,"IPv4Connectivity":3,"Name":"Red no identificada"}]')


@pytest.fixture
def users_pc(monkeypatch: pytest.MonkeyPatch) -> list[DiscoveredDevice]:
    """Las tarjetas, perfiles y puerto 37020 del PC del usuario; devuelve la lista de lo que «responde» por SADP."""
    sadp_answers: list[DiscoveredDevice] = []

    async def nothing(*_a: object, **_k: object) -> list[object]:
        return []

    async def sadp_search(timeout: float, **_kw: object) -> list[sadp.SadpDevice]:
        scanreport.note_sadp_listen(ListenStatus(37020, False, in_use=True, error="[WinError 10013]",
                                                 holders=[PortHolder(6608, "SADPServer.exe", "192.168.1.141")]))
        return [sadp.SadpDevice(host=d.host, model=d.model, serial=d.serial) for d in sadp_answers]

    monkeypatch.setattr(localnet, "local_ipv4", lambda: [localnet.LocalNet("192.168.1.80", 24, "Ethernet"),
                                                         localnet.LocalNet("192.168.1.141", 24, "Wi-Fi")])
    monkeypatch.setattr(winnet, "network_profiles", lambda: parse_profiles(PROFILES_JSON))
    monkeypatch.setattr(discovery, "discover_wsd", nothing)
    monkeypatch.setattr(dhip, "search", nothing)
    monkeypatch.setattr(sadp, "search", sadp_search)
    return sadp_answers


async def test_empty_scan_says_why_and_what_to_do(api: Harness, users_pc: list[DiscoveredDevice]) -> None:
    api.state.discoverer = lambda timeout: discovery.discover(timeout)
    admin = await api.login()
    r = await admin.post("/api/discovery/scan", json={"timeout_s": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["devices"] == []
    report = body["report"]
    assert [(n["name"], n["category"], n["network"]) for n in report["nics"]] == [
        ("Ethernet", "public", "Red no identificada"), ("Wi-Fi", "public", "MiWifi-Casa")]
    assert report["sadp_listen"]["ok"] is False and report["sadp_listen"]["holders"] == [
        {"pid": 6608, "name": "SADPServer.exe", "ip": "192.168.1.141"}]
    busy, eth, wifi = report["warnings"]
    assert busy["code"] == "sadp_port_busy" and "SADPServer.exe" in busy["text_es"]
    assert eth["code"] == wifi["code"] == "public_network"
    assert "«Ethernet»" in eth["text_es"] and "«Pública»" in eth["text_es"] and "Qué hacer:" in eth["text_es"]
    assert eth["command"] == "Set-NetConnectionProfile -InterfaceAlias 'Ethernet' -NetworkCategory Private"
    assert "«Wi-Fi»" in wifi["text_es"]


async def test_scan_with_sadp_answers_only_keeps_the_port_warning(api: Harness,
                                                                  users_pc: list[DiscoveredDevice]) -> None:
    users_pc.append(DiscoveredDevice(host="192.168.1.64", model="DS-2CD1741FWD-IZ", serial="DS-2CD1741FWD-IZ0001"))
    api.state.discoverer = lambda timeout: discovery.discover(timeout)
    admin = await api.login()
    body = (await admin.post("/api/discovery/scan", json={"timeout_s": 1})).json()
    assert [d["host"] for d in body["devices"]] == ["192.168.1.64"]
    assert [w["code"] for w in body["report"]["warnings"]] == ["sadp_port_busy"]


async def test_scan_log_names_each_adapter_once(api: Harness, users_pc: list[DiscoveredDevice],
                                               monkeypatch: pytest.MonkeyPatch,
                                               caplog: pytest.LogCaptureFixture) -> None:
    """Con una IP añadida para llegar a una cámara con la IP de fábrica, la línea de vms.log repetía la tarjeta."""
    monkeypatch.setattr(localnet, "local_ipv4", lambda: [localnet.LocalNet("192.168.1.80", 24, "Ethernet"),
                                                         localnet.LocalNet("192.168.254.250", 24, "Ethernet"),
                                                         localnet.LocalNet("192.168.1.141", 24, "Wi-Fi")])
    api.state.discoverer = lambda timeout: discovery.discover(timeout)
    admin = await api.login()
    with caplog.at_level(logging.INFO, logger="vms.api.devices"):
        body = (await admin.post("/api/discovery/scan", json={"timeout_s": 1})).json()
    assert len(body["report"]["nics"]) == 3 and len(body["report"]["warnings"]) == 3
    (line,) = [r.getMessage() for r in caplog.records if r.name == "vms.api.devices" and "avisos" in r.getMessage()]
    assert line.endswith("(tarjetas: Ethernet=public, Wi-Fi=public)"), line


async def test_scan_report_is_empty_when_the_discoverer_notes_nothing(api: Harness) -> None:
    """Dobles de la API (y cualquier Discoverer que no sea `discover`): el campo existe y no inventa avisos."""
    api.discovered = [DiscoveredDevice(host="10.0.0.99", vendor_guess="dahua")]
    admin = await api.login()
    body = (await admin.post("/api/discovery/scan", json={"timeout_s": 1})).json()
    assert [d["host"] for d in body["devices"]] == ["10.0.0.99"]
    assert body["report"] == {"nics": [], "sadp_listen": None, "warnings": []}
