"""Búsqueda en la red que no encuentra nada: por qué (perfil «Pública» de cada tarjeta y programa que ocupa el
puerto de SADP) y que la escucha de SADP conviva con otro programa cuando Windows lo deja.

Nunca se usan el 37020 ni el 3702 reales (en el PC del usuario los tienen SADPServer.exe de HikCentral y
dasHost.exe): el puerto de escucha es inyectable y las pruebas de bind usan uno libre al azar en 127.0.0.1.
Las reglas de bind que se reproducen son las de Windows con el mismo usuario; entre cuentas distintas (el
servicio del backend frente al de SADPServer) no se pueden probar aquí.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace

import psutil
import pytest

from tests.test_spanish_style import voseo_in
from vms.core.interfaces import DiscoveredDevice
from vms.vendors import dhip, discovery, localnet, sadp, scanreport, winnet
from vms.vendors.scanreport import ListenStatus, NicInfo, ScanReport, explain
from vms.vendors.winnet import NetProfile, PortHolder, parse_profiles

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="reglas de bind de los sockets de Windows")

# Como Get-NetConnectionProfile en el PC del usuario (8/10/2026): el cable de las cámaras y la wifi, las dos
# «Pública». El nombre de la wifi es inventado: el de verdad identifica su casa y el repositorio es público.
PROFILES_JSON = ('[{"InterfaceAlias":"Wi-Fi","InterfaceIndex":8,"NetworkCategory":0,"IPv4Connectivity":4,'
                 '"Name":"MiWifi-Casa"},{"InterfaceAlias":"Ethernet","InterfaceIndex":18,'
                 '"NetworkCategory":0,"IPv4Connectivity":3,"Name":"Red no identificada"}]')
NETS = [localnet.LocalNet("192.168.1.80", 24, "Ethernet"), localnet.LocalNet("192.168.1.141", 24, "Wi-Fi")]
SADPSERVER = PortHolder(6608, "SADPServer.exe", "192.168.1.141")


def _this_pc() -> ScanReport:
    report = ScanReport(nics=[NicInfo(n.ip, n.name) for n in NETS])
    report.add_profiles(parse_profiles(PROFILES_JSON))
    return report


# --------------------------------------------------------------------------- perfil de red
def test_parse_profiles_accepts_a_list_a_single_object_and_ignores_garbage() -> None:
    profiles = parse_profiles(PROFILES_JSON)
    assert profiles["Ethernet"] == NetProfile("Ethernet", 18, "public", False, "Red no identificada")
    assert profiles["Wi-Fi"].internet is True
    # PowerShell 5.1 da un objeto suelto (no una lista) si solo hay una tarjeta; con BOM y CRLF
    one = parse_profiles('﻿{"InterfaceAlias":"Conexión de área local","InterfaceIndex":5,"NetworkCategory":1}\r\n')
    assert one["Conexión de área local"].category == "private"
    assert parse_profiles('[{"InterfaceAlias":"X","NetworkCategory":"DomainAuthenticated"}]')["X"].category == "domain"
    for bad in ("", "no es json", "[1, 2]", '{"InterfaceAlias": "X"}', '[{"NetworkCategory": 0}]',
                '[{"InterfaceAlias":"X","NetworkCategory":true}]', '[{"InterfaceAlias":"X","NetworkCategory":7}]'):
        assert parse_profiles(bad) == {}, bad


def test_internet_is_understood_as_a_number_or_as_text() -> None:
    """Si PowerShell diera las enumeraciones como texto (como ya se acepta para la categoría), todas las tarjetas
    quedaban «sin Internet» y la wifi de casa perdía la advertencia de wifi pública."""
    text = parse_profiles('[{"InterfaceAlias":"Wi-Fi","NetworkCategory":"Public","IPv4Connectivity":"Internet"},'
                          '{"InterfaceAlias":"Ethernet","NetworkCategory":"Public","IPv4Connectivity":"LocalNetwork"}]')
    assert (text["Wi-Fi"].internet, text["Ethernet"].internet) == (True, False)
    for odd in ("true", "null", '"4"', "4.0"):
        one = parse_profiles(f'{{"InterfaceAlias":"X","NetworkCategory":0,"IPv4Connectivity":{odd}}}')
        assert one["X"].internet is False, odd


def test_a_powershell_error_in_clixml_is_logged_as_plain_text(monkeypatch: pytest.MonkeyPatch,
                                                              caplog: pytest.LogCaptureFixture) -> None:
    """Con la salida redirigida, PowerShell 5.1 da a veces los errores en CLIXML: el registro decía solo
    «#< CLIXML» y no se sabía por qué no se pudo leer el perfil de red."""
    clixml = ('#< CLIXML\r\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
              '<Obj S="progress" RefId="0"><TN RefId="0"><T>System.Management.Automation.PSCustomObject</T></TN>'
              '<MS><I64 N="SourceId">1</I64><PR N="Record"><AV>Preparando módulos para el primer uso.</AV></PR></MS>'
              '</Obj><S S="Error">Get-CimInstance : Acceso denegado. _x000D__x000A_</S>'
              '<S S="Error">En línea: 1 Carácter: 89_x000D__x000A_</S></Objs>').encode()
    commands: list[str] = []

    def powershell(args: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(args[-1])
        return subprocess.CompletedProcess(args, 1, b"", clixml)

    monkeypatch.setattr(winnet, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(subprocess, "run", powershell)
    with caplog.at_level(logging.INFO, logger="vms.vendors.winnet"):
        assert winnet.network_profiles() == {}
    (msg,) = [r.getMessage() for r in caplog.records if r.name == "vms.vendors.winnet"]
    assert msg.endswith("código 1: Get-CimInstance : Acceso denegado."), msg
    # y la barra de progreso, que es la que sale en CLIXML, ni se pide
    assert "$ProgressPreference='SilentlyContinue'" in commands[0]
    assert winnet._ps_error("Get-CimInstance : No válido\r\nEn línea: 1".encode()) == "Get-CimInstance : No válido"
    assert winnet._ps_error(b"#< CLIXML\r\n") == winnet._ps_error(b"") == "sin detalle"


def test_an_adapter_with_a_public_and_a_private_profile_counts_as_public() -> None:
    """Una tarjeta puede tener dos perfiles (red IPv4 y red IPv6): manda el «Público», que es el que bloquea."""
    private = '{"InterfaceAlias":"Ethernet","InterfaceIndex":18,"NetworkCategory":1,"Name":"Oficina"}'
    public = '{"InterfaceAlias":"Ethernet","InterfaceIndex":18,"NetworkCategory":0,"Name":"Red no identificada"}'
    for both in (f"[{public},{private}]", f"[{private},{public}]"):
        p = parse_profiles(both)["Ethernet"]
        assert (p.category, p.network) == ("public", "Red no identificada"), both


def test_network_profiles_never_raises_and_reads_again_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def slow() -> str:
        calls.append(1)
        raise subprocess.TimeoutExpired("powershell.exe", winnet.PROFILE_TIMEOUT_S)

    monkeypatch.setattr(winnet, "_query_profiles", slow)
    assert winnet.network_profiles() == {} and winnet.network_profiles() == {}   # «no se sabe», nunca un error
    assert calls == [1, 1]                  # un fallo no se guarda: la búsqueda siguiente lo vuelve a intentar
    monkeypatch.setattr(winnet, "_query_profiles", lambda: PROFILES_JSON)
    assert winnet.network_profiles()["Ethernet"].category == "public"


@windows_only
def test_network_profiles_reads_this_pc_without_admin() -> None:
    """Solo lee (Get-CimInstance); no manda nada a la red ni cambia el perfil. Los alias son los mismos nombres
    de tarjeta que da psutil (y con ellos `local_ipv4()`): si no, el informe nunca encontraría el perfil de una
    tarjeta. Solo tienen perfil las tarjetas conectadas: un PC con solo una tarjeta virtual sin red no tiene
    ninguno, y eso no es un fallo."""
    if not localnet.local_ipv4():
        pytest.skip("este PC no tiene ninguna tarjeta de red con IPv4")
    profiles = winnet.network_profiles()
    if not profiles:
        assert winnet.parse_profiles(winnet._query_profiles()) == {}     # «no hay perfiles», no un error al leer
        pytest.skip("ninguna tarjeta de este PC tiene perfil de red (ninguna está conectada a una red)")
    assert all(isinstance(p, NetProfile) and p.alias == alias for alias, p in profiles.items())
    assert set(profiles) <= set(psutil.net_if_addrs()), (profiles, list(psutil.net_if_addrs()))


# --------------------------------------------------------------------------- avisos
def test_public_network_is_explained_with_the_line_to_fix_it() -> None:
    eth, wifi = explain(_this_pc(), [])
    assert eth.code == wifi.code == "public_network"
    # primero la tarjeta sin Internet: suele ser el switch de las cámaras
    assert ("Windows tiene como «Pública» la red de la conexión «Ethernet» (192.168.1.80, Red no identificada) de "
            "este PC") in eth.text_es
    assert eth.command == "Set-NetConnectionProfile -InterfaceAlias 'Ethernet' -NetworkCategory Private"
    assert "al reiniciar el PC" in eth.text_es and "wifi pública" not in eth.text_es
    # la línea va justo después del texto, que acaba presentándola (no tras la nota del reinicio)
    assert eth.text_es.endswith("Para ponerla como «Privada», abre PowerShell como administrador (botón derecho en "
                                "el botón Inicio → «Terminal (administrador)» o «Windows PowerShell "
                                "(administrador)»), pega esta línea y vuelve a buscar:")
    assert "«Wi-Fi» (192.168.1.141, MiWifi-Casa)" in wifi.text_es
    assert "wifi pública" in wifi.text_es and "al reiniciar" not in wifi.text_es
    assert wifi.command.endswith("-InterfaceAlias 'Wi-Fi' -NetworkCategory Private")


def test_one_public_warning_per_adapter_even_with_several_ips() -> None:
    """`discover()` anota una entrada por IP. Con una IP fija y otra añadida para llegar a una cámara con la IP de
    fábrica (192.168.254.x), la misma tarjeta daba dos avisos iguales y podía dejar fuera el de otra tarjeta."""
    nics = [NicInfo("192.168.1.80", "Ethernet", "public", "Red no identificada", False, 18),
            NicInfo("192.168.254.250", "Ethernet", "public", "Red no identificada", False, 18)]
    (eth,) = explain(ScanReport(nics=nics), [])
    assert "«Ethernet» (192.168.1.80, 192.168.254.250, Red no identificada)" in eth.text_es
    assert eth.command == "Set-NetConnectionProfile -InterfaceAlias 'Ethernet' -NetworkCategory Private"
    # el límite cuenta tarjetas, no IP: la cuarta tarjeta es la que se queda fuera
    others = [NicInfo(f"10.0.{i}.5", f"Ethernet {i}", "public", internet=True, index=20 + i) for i in (2, 3, 4)]
    warned = explain(ScanReport(nics=nics + others), [])
    assert len(warned) == scanreport.MAX_PUBLIC_WARNINGS == 3
    for w, name in zip(warned, ("«Ethernet» (", "«Ethernet 2» (", "«Ethernet 3» ("), strict=True):
        assert name in w.text_es, (name, w.text_es)


def test_virtual_adapters_never_push_the_cameras_one_out() -> None:
    """Hyper-V/WSL («vEthernet»), VMware y VirtualBox crean tarjetas «Red no identificada», «Pública» y sin
    Internet, igual que el switch de las cámaras. Con tres de ellas delante, el aviso del cable de las cámaras se
    quedaba fuera por el límite y al usuario solo se le pedía cambiar las redes virtuales."""
    virtual = [NicInfo("172.20.160.1", "vEthernet (WSL)", "public", "Red no identificada", False, 40),
               NicInfo("172.25.0.1", "vEthernet (Default Switch)", "public", "Red no identificada", False, 41),
               NicInfo("192.168.56.1", "VirtualBox Host-Only Network", "public", "Red no identificada", False, 42),
               NicInfo("192.168.152.1", "VMware Network Adapter VMnet8", "public", "Red no identificada", False, 43)]
    cable = NicInfo("192.168.1.80", "Ethernet", "public", "Red no identificada", False, 18)
    warned = explain(ScanReport(nics=[*virtual, cable]), [])
    assert len(warned) == scanreport.MAX_PUBLIC_WARNINGS
    assert "«Ethernet» (192.168.1.80" in warned[0].text_es
    # y una tarjeta de verdad con Internet (la wifi) también va antes que las virtuales
    wifi = NicInfo("192.168.1.141", "Wi-Fi", "public", "MiWifi-Casa", True, 8)
    warned = explain(ScanReport(nics=[*virtual, wifi, cable]), [])
    assert ["«Ethernet»" in warned[0].text_es, "«Wi-Fi»" in warned[1].text_es] == [True, True]


def test_a_wifi_without_internet_still_gets_the_public_wifi_caution() -> None:
    """La wifi de un bar o un hotel con portal de acceso (sin aceptar aún) no tiene Internet para Windows: antes no
    salía «nunca si es una wifi pública» justo donde más falta hace."""
    hotel = NicInfo("10.10.4.27", "Wi-Fi", "public", "Hotel-Invitados", False, 8)
    (w,) = explain(ScanReport(nics=[hotel]), [])
    assert "nunca si es una wifi pública" in w.text_es
    assert "al reiniciar" not in w.text_es          # Windows recuerda una wifi por su nombre
    cable = NicInfo("192.168.1.80", "Ethernet", "public", "Red no identificada", False, 18)
    (w,) = explain(ScanReport(nics=[cable]), [])
    assert "wifi pública" not in w.text_es and "al reiniciar" in w.text_es


def test_no_public_warning_when_sadp_answered_or_the_network_is_private_or_unknown() -> None:
    assert explain(_this_pc(), [DiscoveredDevice(host="192.168.1.64", sources=["sadp", "wsd"])]) == []
    # solo ONVIF: las respuestas SADP (multicast al 37020) pueden ser las que bloquea el firewall
    assert len(explain(_this_pc(), [DiscoveredDevice(host="192.168.1.70", sources=["wsd"])])) == 2
    assert explain(ScanReport(nics=[NicInfo("192.168.1.80", "Ethernet", category="private")]), []) == []
    assert explain(ScanReport(nics=[NicInfo("192.168.1.80", "Ethernet")]), []) == []     # sin datos: nada inventado
    shared = ListenStatus(37020, True, "per_nic", in_use=True, error="[WinError 10013]")
    assert explain(ScanReport(sadp_listen=shared), []) == []


def test_sadp_port_held_by_another_program_names_it() -> None:
    busy = ListenStatus(37020, False, in_use=True, error="[WinError 10013]", holders=[SADPSERVER])
    # se avisa aunque SADP haya respondido (por unicast): pueden faltar otras cámaras
    (w,) = explain(ScanReport(sadp_listen=busy), [DiscoveredDevice(host="192.168.1.64", sources=["sadp"])])
    assert w.code == "sadp_port_busy" and w.command == ""
    assert "puerto UDP 37020" in w.text_es and "SADPServer.exe (la búsqueda SADP de iVMS-4200 o HikCentral" in w.text_es
    assert "«Añadir equipo»" in w.text_es
    hidden = explain(ScanReport(sadp_listen=ListenStatus(37020, False, in_use=True, holders=[PortHolder(4, "")])), [])
    assert hidden[0].code == "sadp_port_busy" and "no deja ver cuál" in hidden[0].text_es
    other = explain(ScanReport(sadp_listen=ListenStatus(37020, False, in_use=True, holders=[PortHolder(9, "otro.exe")])),
                    [])
    assert "otro.exe" in other[0].text_es and "iVMS" not in other[0].text_es.split("Qué hacer:")[1]
    failed = explain(ScanReport(sadp_listen=ListenStatus(37020, False, error="sin grupo multicast")), [])
    assert failed[0].code == "sadp_listen_failed"


def test_odd_adapter_names_never_produce_a_wrong_command() -> None:
    # «*» es un comodín de -InterfaceAlias: podría cambiar otras tarjetas; con el número no hay duda
    virtual = scanreport._public_warning([NicInfo("169.254.3.4", "Conexión de área local* 1", "public", index=12)])
    assert virtual.command == "Set-NetConnectionProfile -InterfaceIndex 12 -NetworkCategory Private"
    quoted = scanreport._public_warning([NicInfo("10.0.0.5", "Red 'casa'", "public")])
    assert quoted.command == "" and "Tipo de perfil de red" in quoted.text_es
    # PowerShell toma ’ (la comilla tipográfica que pone el autocorrector) como comilla simple: cerraría la cadena
    for name in ("Cámaras d’abajo", "Cámaras ‘abajo", "Cámaras ‚abajo", "Cámaras ‛abajo"):
        curly = scanreport._private_command(NicInfo("10.0.0.6", name, "public", index=7))
        assert curly == "Set-NetConnectionProfile -InterfaceIndex 7 -NetworkCategory Private", name


def test_every_message_says_what_to_do_in_neutral_spanish() -> None:
    reports = [
        _this_pc(),
        ScanReport(nics=[NicInfo("10.0.0.5", "Red 'casa'", "public", internet=None)]),
        ScanReport(sadp_listen=ListenStatus(37020, False, in_use=True, holders=[SADPSERVER])),
        ScanReport(sadp_listen=ListenStatus(37020, False, in_use=True, holders=[PortHolder(9, "otro.exe")])),
        ScanReport(sadp_listen=ListenStatus(37020, False, in_use=True)),
        ScanReport(sadp_listen=ListenStatus(37020, False, error="x")),
    ]
    texts = [w.text_es for r in reports for w in explain(r, [])]
    assert len(texts) == 7
    for t in texts:
        assert "Qué hacer: " in t and not voseo_in(t), t


# --------------------------------------------------------------------------- discover() rellena el informe
async def test_discover_fills_the_report_only_when_the_api_opens_one(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[int] = []

    def profiles() -> dict[str, NetProfile]:
        reads.append(1)
        return parse_profiles(PROFILES_JSON)

    async def nothing(*_a: object, **_k: object) -> list[object]:
        return []

    async def sadp_search(timeout: float, **kw: object) -> list[object]:
        assert kw["interfaces"] == ["192.168.1.80", "192.168.1.141"] and kw["multicast"] is True
        scanreport.note_sadp_listen(ListenStatus(37020, False, in_use=True, error="x", holders=[SADPSERVER]))
        return []

    monkeypatch.setattr(localnet, "local_ipv4", lambda: list(NETS))
    monkeypatch.setattr(winnet, "network_profiles", profiles)
    monkeypatch.setattr(discovery, "discover_wsd", nothing)
    monkeypatch.setattr(dhip, "search", nothing)
    monkeypatch.setattr(sadp, "search", sadp_search)
    # la búsqueda periódica de cambios de IP no abre informe: ni se lanza PowerShell
    assert await discovery.discover(0.2) == [] and reads == []
    with scanreport.collecting() as report:
        assert await discovery.discover(0.2) == []
    assert scanreport.current() is None and reads == [1]
    assert [(n.name, n.category, n.internet) for n in report.nics] == [("Ethernet", "public", False),
                                                                       ("Wi-Fi", "public", True)]
    assert report.sadp_listen is not None and report.sadp_listen.holders == [SADPSERVER]
    assert [w["code"] for w in report.as_dict([])["warnings"]] == ["sadp_port_busy", "public_network",
                                                                   "public_network"]


async def test_the_search_after_making_the_network_private_no_longer_says_public(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """El aviso dice «pega esta línea y vuelve a buscar». Con el perfil guardado 60 s, la búsqueda de justo después
    seguía diciendo «Pública», con la misma línea: parecía que el cambio no servía y tapaba la causa de verdad
    (cámara apagada, cable, otro switch)."""
    category = ["0"]        # NetworkCategory de Windows: 0 «Pública», 1 «Privada»

    def powershell() -> str:
        return PROFILES_JSON.replace('"NetworkCategory":0', f'"NetworkCategory":{category[0]}')

    async def nothing(*_a: object, **_k: object) -> list[object]:
        return []

    monkeypatch.setattr(localnet, "local_ipv4", lambda: list(NETS))
    monkeypatch.setattr(winnet, "_query_profiles", powershell)      # winnet.network_profiles() de verdad
    monkeypatch.setattr(discovery, "discover_wsd", nothing)
    monkeypatch.setattr(dhip, "search", nothing)
    monkeypatch.setattr(sadp, "search", nothing)
    with scanreport.collecting() as first:
        assert await discovery.discover(0.2) == []
    assert [w["code"] for w in first.as_dict([])["warnings"]] == ["public_network", "public_network"]
    category[0] = "1"       # el usuario pega Set-NetConnectionProfile … -NetworkCategory Private
    with scanreport.collecting() as second:
        assert await discovery.discover(0.2) == []
    assert [n.category for n in second.nics] == ["private", "private"]
    assert second.as_dict([])["warnings"] == []


async def test_sadp_search_notes_the_listener_in_the_open_report(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_udp_port()
    status = ListenStatus(port, False, in_use=True, error="x")
    asked: list[int] = []

    def listen(interfaces: list[str | None], p: int) -> tuple[list[socket.socket], ListenStatus]:
        asked.append(p)
        return [], status

    monkeypatch.setattr(sadp, "_listen_sockets", listen)
    with scanreport.collecting() as report:
        assert await sadp.search(0.2, interfaces=["127.0.0.1"], port=port) == []
    assert asked == [port] and report.sadp_listen is status


def test_listen_failure_is_a_warning_once_and_debug_while_it_repeats(monkeypatch: pytest.MonkeyPatch,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    """En todos los sistemas (la CI de Linux también lo comprueba): el puerto ocupado va al registro como WARNING
    con quién lo tiene, no como DEBUG. La búsqueda de cambios de IP lo repite cada 120 s mientras una cámara con
    «seguir IP» no da vídeo: el mismo problema no se vuelve a avisar hasta que cambia."""
    holders = [PortHolder(6608, "SADPServer.exe", "0.0.0.0")]

    def taken(ip: str, port: int) -> socket.socket:
        raise PermissionError(errno.EACCES, "Intento de acceso a un socket no permitido por sus permisos")

    monkeypatch.setattr(sadp, "_bind_shared", taken)
    monkeypatch.setattr(winnet, "udp_port_holders", lambda port: list(holders))
    monkeypatch.setattr(sadp, "_last_noted", None, raising=False)

    def listen() -> tuple[ListenStatus, list[tuple[int, str]]]:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="vms.vendors.sadp"):
            socks, st = sadp._listen_sockets(["127.0.0.1"], 50123)
        assert socks == []
        return st, [(r.levelno, r.getMessage()) for r in caplog.records if "puerto UDP 50123" in r.getMessage()]

    st, logged = listen()
    assert (st.ok, st.mode, st.in_use, st.holders) == (False, "", True, holders)
    assert [level for level, _ in logged] == [logging.WARNING]
    assert "lo tiene: SADPServer.exe (pid 6608, 0.0.0.0)" in logged[0][1]
    assert [level for level, _ in listen()[1]] == [logging.DEBUG]       # lo mismo otra vez: no llena el registro
    holders[0] = PortHolder(7001, "iVMS-4200.Framework.C.exe", "0.0.0.0")
    assert [level for level, _ in listen()[1]] == [logging.WARNING]     # otro programa: se avisa de nuevo


def test_per_nic_fallback_log_says_what_it_saw_not_that_the_port_is_taken_on_every_ip(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Bajo NT SERVICE\\VMSBackend el bind en 0.0.0.0 puede fallar aunque SADPServer.exe escuche solo en la IP de la
    wifi (cuentas distintas). El registro decía «lo tiene otro programa en todas las IP»: falso, y despistaba a quien
    leía vms.log. En todos los sistemas: el modo por tarjeta (solo Windows) se simula con dobles."""
    def bind(ip: str, port: int) -> socket.socket:
        if not ip:
            raise PermissionError(errno.EACCES, "Intento de acceso a un socket no permitido por sus permisos")
        return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    monkeypatch.setattr(sadp, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(sadp, "_bind_shared", bind)
    monkeypatch.setattr(sadp, "_join", lambda sock, iface: True)
    monkeypatch.setattr(winnet, "udp_port_holders", lambda port: [SADPSERVER])
    monkeypatch.setattr(sadp, "_last_noted", None)
    with caplog.at_level(logging.INFO, logger="vms.vendors.sadp"):
        socks, st = sadp._listen_sockets(["192.168.1.80", "192.168.1.141"], 50124)
    for s in socks:
        s.close()
    assert (st.ok, st.mode, len(socks)) == (True, "per_nic", 2)
    (msg,) = [r.getMessage() for r in caplog.records if "puerto UDP 50124" in r.getMessage()]
    assert "en todas las IP" in msg and "lo tiene otro programa en todas las IP" not in msg
    assert "lo usa: SADPServer.exe (pid 6608, 192.168.1.141)" in msg and "Intento de acceso" in msg


async def test_the_listener_is_opened_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si el bind falla, psutil repasa todas las conexiones del PC y el nombre de cada proceso para decir quién
    tiene el puerto. Eso se hacía en el bucle de eventos y paraba el backend (la web, el vídeo) mientras tanto."""
    seen: list[int] = []

    def listen(interfaces: list[str | None], p: int) -> tuple[list[socket.socket], ListenStatus]:
        seen.append(threading.get_ident())
        return [], ListenStatus(p, False, in_use=True, error="x")

    monkeypatch.setattr(sadp, "_listen_sockets", listen)
    assert await sadp.search(0.2, interfaces=["127.0.0.1"], port=_free_udp_port()) == []
    assert seen and seen[0] != threading.get_ident()


async def test_a_cancelled_search_closes_the_listeners_opened_meanwhile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fuera del bucle, la escucha puede acabar después de cancelar la búsqueda: sus sockets no se quedan abiertos
    (tendrían el puerto hasta que los recogiera el recolector de basura)."""
    started, release = threading.Event(), threading.Event()
    opened: list[socket.socket] = []

    def listen(interfaces: list[str | None], p: int) -> tuple[list[socket.socket], ListenStatus]:
        started.set()
        release.wait(5)
        opened.append(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        return list(opened), ListenStatus(p, True, "all")

    monkeypatch.setattr(sadp, "_listen_sockets", listen)
    task = asyncio.create_task(sadp.search(0.2, interfaces=["127.0.0.1"], port=_free_udp_port()))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(200):
        if opened and opened[0].fileno() == -1:
            break
        await asyncio.sleep(0.01)
    assert opened and opened[0].fileno() == -1


def test_wsd_never_binds_the_ws_discovery_port() -> None:
    """dasHost.exe tiene 0.0.0.0:3702 en Windows: la sonda sale de un puerto cualquiera."""
    for iface in (None, "127.0.0.1"):
        with discovery._make_socket(iface) as s:
            assert s.getsockname()[1] not in (0, discovery.MULTICAST_ADDR[1])


# --------------------------------------------------------------------------- reglas de bind (Windows)
def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    assert port not in (sadp.SADP_PORT, 3702)
    return port


def _holder(ip: str, port: int, *, exclusive: bool = False) -> socket.socket:
    """El «otro programa»: un socket sin SO_REUSEADDR (o exclusivo) en `port`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    if exclusive:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
    s.bind((ip, port))
    return s


@windows_only
def test_listener_shares_the_port_with_a_program_bound_to_one_ip() -> None:
    """El caso del PC del usuario: SADPServer.exe escucha en 192.168.1.141:37020 (la IP de la wifi)."""
    port = _free_udp_port()
    holder = _holder("127.0.0.1", port)
    try:
        socks, st = sadp._listen_sockets(["127.0.0.1"], port)
        try:
            assert st.ok and st.mode == "all" and not st.in_use
            assert [s.getsockname() for s in socks] == [("0.0.0.0", port)]
        finally:
            for s in socks:
                s.close()
    finally:
        holder.close()


@windows_only
def test_listener_falls_back_to_one_socket_per_nic_when_the_port_is_taken_on_all_ips(
        caplog: pytest.LogCaptureFixture) -> None:
    """Otro programa en 0.0.0.0 sin compartir: el bind en 0.0.0.0 da WSAEACCES, pero por IP de tarjeta sí."""
    port = _free_udp_port()
    holder = _holder("", port)
    try:
        with caplog.at_level(logging.INFO, logger="vms.vendors.sadp"):
            socks, st = sadp._listen_sockets(["127.0.0.1"], port)
        try:
            assert st.ok and st.mode == "per_nic" and st.in_use
            assert [s.getsockname() for s in socks] == [("127.0.0.1", port)]
            # quién lo tenía: el otro (aquí, este mismo proceso en 0.0.0.0), no nuestro socket nuevo
            assert [(h.pid, h.ip) for h in st.holders] == [(os.getpid(), "0.0.0.0")]
            for s in (*socks, holder):
                s.settimeout(2)
            # lo que llega directo a esa IP va al socket más concreto, el nuestro (CONTRATO §5.1)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                tx.sendto(b"<directa/>", ("127.0.0.1", port))
            assert socks[0].recvfrom(100)[0] == b"<directa/>"
            # y el multicast, que es como llegan las respuestas SADP, llega a los dos: escuchamos de verdad y el
            # otro programa no se queda sin él. Por la tarjeta de bucle local y con TTL 0: no sale del PC.
            holder.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                              struct.pack("4s4s", socket.inet_aton(sadp.SADP_GROUP), socket.inet_aton("127.0.0.1")))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
                tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
                tx.sendto(b"<multicast/>", (sadp.SADP_GROUP, port))
            assert socks[0].recvfrom(100)[0] == b"<multicast/>"
            assert holder.recvfrom(100)[0] == b"<multicast/>"
        finally:
            for s in socks:
                s.close()
    finally:
        holder.close()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@windows_only
async def test_listener_that_cannot_share_says_who_has_the_port_and_warns(monkeypatch: pytest.MonkeyPatch,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    """Con SO_EXCLUSIVEADDRUSE no hay forma: el informe y el registro (WARNING, no DEBUG) dicen quién lo tiene."""
    monkeypatch.setattr(sadp, "_last_noted", None)
    port = _free_udp_port()
    holder = _holder("", port, exclusive=True)
    me = psutil.Process().name()
    try:
        with caplog.at_level(logging.WARNING, logger="vms.vendors.sadp"), scanreport.collecting() as report:
            found = await sadp.search(0.2, interfaces=["127.0.0.1"], port=port)
    finally:
        holder.close()
    assert found == []
    st = report.sadp_listen
    assert st is not None and not st.ok and st.in_use and "10013" in st.error
    assert PortHolder(os.getpid(), me, "0.0.0.0") in st.holders
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"puerto UDP {port}" in m and me in m for m in warnings), warnings
    (w,) = explain(report, [])
    assert w.code == "sadp_port_busy" and me in w.text_es
