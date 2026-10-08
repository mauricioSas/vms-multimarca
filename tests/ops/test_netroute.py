"""Paso `route` de «¿Por qué no conecta?»: por qué tarjeta de red sale este PC hacia el equipo (CONTRATO §18.11).

Las fotos de red son las del PC de la tienda (8/10/2026, leídas con psutil y el truco UDP, sin enviar nada):
- `cable_out()`: «Ethernet» sin enlace pero con su IP fija 192.168.1.80/24 (y una 169.254 de enlace local),
  «Wi-Fi» 192.168.1.141/24 con enlace, Bluetooth y WiFi Direct sin enlace con 169.254; Windows sale hacia la cámara
  (192.168.1.64) por «Wi-Fi».
- `cable_in()`: las dos con enlace en 192.168.1.0/24; «Ethernet» (métrica 25) gana a «Wi-Fi» (30).
"""
from __future__ import annotations

import socket
import sys
import types
from typing import Any

import pytest

from tests.test_spanish_style import voseo_in
from vms.ops import netroute
from vms.ops.netroute import Adapter, Addr, NetView, assess

CAM = "192.168.1.64"
WIFI = Adapter("Wi-Fi", True, (Addr("192.168.1.141", 24),))
ETH_DOWN = Adapter("Ethernet", False, (Addr("169.254.108.194", 16), Addr("192.168.1.80", 24)))
ETH_UP = Adapter("Ethernet", True, (Addr("192.168.1.80", 24),))
VIRTUAL = (Adapter("Conexión de área local* 1", False, (Addr("169.254.82.151", 16),)),
           Adapter("Conexión de área local* 2", False, (Addr("169.254.174.171", 16),)),
           Adapter("Conexión de red Bluetooth", False, (Addr("169.254.198.172", 16),)))


def cable_out() -> NetView:
    return NetView((ETH_DOWN, VIRTUAL[0], VIRTUAL[1], WIFI, VIRTUAL[2]), "192.168.1.141")


def cable_in(source: str = "192.168.1.80") -> NetView:
    return NetView((ETH_UP, VIRTUAL[0], VIRTUAL[1], WIFI, VIRTUAL[2]), source)


# --------------------------------------------------------------------------- estado 1: cable desconectado
def test_unplugged_cable_is_the_cause_when_the_camera_does_not_answer() -> None:
    chk = assess(CAM, cable_out(), answered=False)
    assert chk is not None and chk.reason == "link_down" and chk.ok is False
    assert "«Ethernet»" in chk.detail_es and "192.168.1.80" in chk.detail_es and "desconectada" in chk.detail_es
    assert "Por eso busca el equipo por «Wi-Fi» (192.168.1.141), y por ahí no contesta." in chk.detail_es
    assert chk.action_es.startswith("Conecta el cable de red de «Ethernet»")
    # por si el equipo va por WiFi y la tarjeta del cable no se usa (revisión del 8/10, caso d)
    assert "Si el equipo no va por ese cable sino por «Wi-Fi»" in chk.action_es
    # las 169.254 de Bluetooth y WiFi Direct no se nombran
    assert "Bluetooth" not in chk.detail_es and "área local" not in chk.detail_es


def test_unplugged_cable_alone_on_the_camera_network() -> None:
    """Solo «Ethernet» está en la red de la cámara y no tiene enlace; el WiFi es de otra red."""
    view = NetView((ETH_DOWN, Adapter("Wi-Fi", True, (Addr("10.0.0.7", 24),))), "10.0.0.7")
    chk = assess(CAM, view, answered=False)
    assert chk is not None and chk.reason == "link_down" and chk.ok is False
    assert "«Ethernet»" in chk.detail_es and "por «Wi-Fi» (10.0.0.7)" in chk.detail_es
    assert "Si el equipo no va por ese cable" not in chk.action_es   # el WiFi no está en su red


def test_unplugged_cable_does_not_matter_if_the_camera_answers() -> None:
    chk = assess(CAM, cable_out(), answered=True)
    assert chk is not None and chk.ok is True and not chk.warning and chk.reason == "direct"
    assert "«Wi-Fi» (192.168.1.141)" in chk.detail_es


# --------------------------------------------------------------------------- estado 2: las dos con enlace
def test_both_cards_up_and_ethernet_wins_is_only_a_warning() -> None:
    chk = assess(CAM, cable_in(), answered=True)
    assert chk is not None and chk.reason == "shared_subnet" and chk.ok is True and chk.warning
    assert "por «Ethernet» (192.168.1.80)" in chk.detail_es and "«Wi-Fi» (192.168.1.141)" in chk.detail_es
    assert "192.168.1.x" in chk.detail_es
    # separar la red de las cámaras: 192.168.2.x, la primera que no usa ninguna tarjeta
    assert "en «Ethernet» la IP 192.168.2.80" in chk.action_es and "al equipo la 192.168.2.64" in chk.action_es
    assert "mismo router, no pasa nada" in chk.action_es


def test_both_cards_up_but_windows_still_uses_wifi() -> None:
    """El minuto después de conectar el cable (caché de rutas) o un enlace de 100 Mbit/s: Windows sale por
    «Wi-Fi» y por ahí la cámara no contesta. Aviso, no la causa: también puede estar apagada."""
    chk = assess(CAM, cable_in("192.168.1.141"), answered=False)
    assert chk is not None and chk.reason == "shared_subnet" and chk.ok is None and chk.warning
    assert "Windows busca el equipo por «Wi-Fi» (192.168.1.141) y por ahí no contesta" in chk.detail_es
    assert "«Ethernet» (192.168.1.80) también está en su red" in chk.detail_es
    assert "Si el equipo está conectado por «Ethernet» y no por «Wi-Fi»" in chk.action_es
    assert "espera un minuto" in chk.action_es and "desactiva un momento «Wi-Fi»" in chk.action_es
    # no se sabe cuál es la del router: el ejemplo no le pone IP fija a ninguna tarjeta concreta
    assert "deja como está la tarjeta del router" in chk.action_es and "192.168.2.x" in chk.action_es
    assert "la IP 192.168.2." not in chk.action_es


def test_camera_off_with_the_right_card_is_never_blamed_on_the_network() -> None:
    """Revisión del 8/10, caso a: cable puesto, Windows sale por «Ethernet» y la cámara está apagada. El aviso de
    red compartida no puede ser la causa probable (ok False)."""
    chk = assess(CAM, cable_in(), answered=False)
    assert chk is not None and chk.ok is not False and chk.warning


# --------------------------------------------------------------------------- otros casos
def test_link_local_camera_ignores_disconnected_virtual_cards() -> None:
    """Revisión del 8/10, caso c: switch sin router, PC y cámara en 169.254. Bluetooth y WiFi Direct sin enlace
    también tienen una 169.254: no son «la tarjeta de la red del equipo» ni una red compartida."""
    eth = Adapter("Ethernet", True, (Addr("169.254.108.194", 16),))
    view = NetView((eth, *VIRTUAL), "169.254.108.194")
    for answered in (False, True):
        chk = assess("169.254.5.5", view, answered=answered)
        assert chk is not None and chk.reason == "direct" and chk.ok is True and not chk.warning


def test_camera_on_another_network() -> None:
    """La IP de fábrica de la cámara del usuario (192.168.254.27) con el PC en 192.168.1.x."""
    chk = assess("192.168.254.27", cable_in("192.168.1.141"), answered=False)
    assert chk is not None and chk.reason == "other_network" and chk.ok is False
    assert "192.168.254.27" in chk.detail_es and "192.168.1.80" in chk.detail_es
    assert "192.168.254.250" in chk.action_es and "«Editar»" in chk.action_es
    # detrás de un router y contestando: no es un problema
    chk = assess("192.168.254.27", cable_in("192.168.1.141"), answered=True)
    assert chk is not None and chk.reason == "router" and chk.ok is True


def test_pc_without_any_network() -> None:
    view = NetView((ETH_DOWN, Adapter("Wi-Fi", False, (Addr("169.254.3.3", 16),))), None)
    chk = assess("10.0.0.5", view, answered=False)
    assert chk is not None and chk.ok is False and "ninguna red" in chk.detail_es


@pytest.mark.parametrize("host", ["cam-caja.tienda.local", "127.0.0.1", "8.8.8.8", "fe80::1", "", "0.0.0.0"])
def test_only_lan_ipv4_addresses_are_checked(host: str) -> None:
    assert assess(host, cable_out(), answered=False) is None


def _all_checks() -> list[Any]:
    cases = [(CAM, cable_out(), False), (CAM, cable_out(), True), (CAM, cable_in(), True),
             (CAM, cable_in("192.168.1.141"), False), (CAM, cable_in(), False),
             ("192.168.254.27", cable_in("192.168.1.141"), False), ("169.254.5.5", cable_out(), False),
             ("10.0.0.5", NetView((ETH_DOWN,), None), False),
             (CAM, NetView((ETH_UP, WIFI), "10.8.0.2"), False),
             (CAM, NetView((ETH_UP, WIFI, Adapter("VPN", True, (Addr("10.8.0.2", 24),))), "10.8.0.2"), False)]
    return [assess(h, v, answered=a) for h, v, a in cases]


def test_every_message_is_neutral_spanish_and_says_what_to_do() -> None:
    checks = [c for c in _all_checks() if c is not None]
    assert {c.reason for c in checks} >= {"link_down", "direct", "shared_subnet", "other_network", "other_route"}
    for c in checks:
        assert not voseo_in(c.detail_es + " " + c.action_es), c
        if c.ok is False or c.warning:
            assert c.action_es, c                     # la web le pone delante «Qué hacer:»


# --------------------------------------------------------------------------- lectura del sistema
def _fake_psutil(addrs: dict[str, list[tuple[int, str, str | None]]], up: dict[str, bool]) -> types.ModuleType:
    mod = types.ModuleType("psutil")

    class A:
        def __init__(self, family: int, address: str, netmask: str | None) -> None:
            self.family, self.address, self.netmask = family, address, netmask

    class S:
        def __init__(self, isup: bool) -> None:
            self.isup = isup

    mod.net_if_addrs = lambda: {n: [A(*e) for e in es] for n, es in addrs.items()}  # type: ignore[attr-defined]
    mod.net_if_stats = lambda: {n: S(v) for n, v in up.items()}  # type: ignore[attr-defined]
    return mod


def test_reader_keeps_disconnected_cards_with_their_fixed_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """psutil en el PC de la tienda con el cable desconectado: «Ethernet» sin enlace pero con 192.168.1.80/24.
    `localnet.local_ipv4()` la descarta (por eso el aviso de «otra red» no salía); aquí tiene que estar."""
    fake = _fake_psutil({
        "Ethernet": [(socket.AF_INET, "169.254.108.194", "255.255.0.0"), (socket.AF_INET, "192.168.1.80",
                                                                          "255.255.255.0"),
                     (getattr(socket, "AF_INET6", 23), "fe80::1", None)],
        "Wi-Fi": [(socket.AF_INET, "192.168.1.141", "255.255.255.0")],
        "Loopback Pseudo-Interface 1": [(socket.AF_INET, "127.0.0.1", "255.0.0.0")],
        "Sin IPv4": [(getattr(socket, "AF_INET6", 23), "fe80::2", None)],
    }, {"Ethernet": False, "Wi-Fi": True, "Loopback Pseudo-Interface 1": True})
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(netroute, "source_ip", lambda host: "192.168.1.141")
    view = netroute.snapshot(CAM)
    assert view is not None and view.source_ip == "192.168.1.141"
    by = {a.name: a for a in view.adapters}
    assert set(by) == {"Ethernet", "Wi-Fi"}
    assert by["Ethernet"].up is False and Addr("192.168.1.80", 24) in by["Ethernet"].addrs
    assert assess(CAM, view, answered=False).reason == "link_down"  # type: ignore[union-attr]


def test_reader_failure_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = types.ModuleType("psutil")

    def boom() -> Any:
        raise OSError("sin permiso")
    broken.net_if_addrs = boom  # type: ignore[attr-defined]
    broken.net_if_stats = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psutil", broken)
    assert netroute.snapshot(CAM) is None


def test_source_ip_sends_nothing_and_works_on_loopback() -> None:
    """El truco UDP en este PC, sin red: hacia 127.0.0.1 sale por 127.0.0.1. Con un nombre no se hace (sería una
    consulta DNS)."""
    assert netroute.source_ip("127.0.0.1") == "127.0.0.1"
    assert netroute.source_ip("cam-caja.tienda.local") is None and netroute.source_ip("fe80::1") is None


async def test_probe_reads_this_pc_without_failing() -> None:
    view = await netroute.probe("127.0.0.1")
    assert view is None or (isinstance(view.adapters, tuple) and view.source_ip == "127.0.0.1")
