"""Descubrimiento SADP (Hikvision, solo búsqueda) y DHIP (Dahua, adaptado de rroller/dahua) y fusión con WSD."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from tools.mocks.dhip import DhipResponder, DhipTarget
from tools.mocks.onvif import brand_scopes
from tools.mocks.sadp import SadpResponder, SadpTarget
from tools.mocks.wsdiscovery import ProbeTarget, WsDiscoveryResponder
from vms.vendors import dhip, discover, sadp
from vms.vendors.netguard import RateLimiter, is_lan_destination


async def test_sadp_search_only_inquiry() -> None:
    with SadpResponder([SadpTarget(host="192.168.1.64"), SadpTarget(host="192.168.1.65", serial="X2", mac="c4-2f-90-00-00-02")]) as r:
        found = await sadp.search(0.5, targets=[("127.0.0.1", r.port)], multicast=False)
    assert sorted(d.host for d in found) == ["192.168.1.64", "192.168.1.65"]
    d = next(x for x in found if x.host == "192.168.1.64")
    assert d.model == "DS-2CD2143G2-I" and d.mac == "c4:2f:90:f1:e6:a1" and d.activated is True
    assert r.probes_received == 2 and r.other_messages == 0          # solo «inquiry», dos envíos


def test_sadp_parser_ignores_other_probes() -> None:
    from tools.mocks.sadp import probe_match
    data = probe_match("UUID-A", SadpTarget(host="10.0.0.9"))
    assert sadp.parse_probe_match(data, "UUID-A") is not None
    assert sadp.parse_probe_match(data, "UUID-B") is None
    assert sadp.parse_probe_match(b"<Probe/>", None) is None
    assert sadp.parse_probe_match(b"no xml", None) is None
    assert b"activate" not in sadp.build_inquiry("X").lower()


def test_dhip_frame_matches_the_original() -> None:
    probe = dhip.build_probe()
    assert probe[:4] == (32).to_bytes(4, "little") and probe[4:8] == b"DHIP"
    body = probe[32:]
    assert json.loads(body) == {"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}}
    assert int.from_bytes(probe[16:24], "little") == len(body) == int.from_bytes(probe[24:32], "little")


def test_dhip_parse_reply() -> None:
    from tools.mocks.dhip import reply
    info = dhip.parse_reply(reply(DhipTarget(host="192.168.1.108")))
    assert info["SerialNo"] == "6J0123PAZ12345" and info["DeviceType"].startswith("DHI-NVR")
    dev = dhip.to_device(info, "127.0.0.1")
    assert dev.host == "192.168.1.108" and dev.channels == 8 and dev.mac == "3c:ef:8c:12:34:56"
    assert dhip.parse_reply(b"") == {} and dhip.parse_reply(b"\x00garbage{") == {}
    assert dhip.parse_reply(b'{"params": {"deviceInfo": {}}}') == {}


async def test_dhip_search_respects_rate_limit_and_lan() -> None:
    with DhipResponder([DhipTarget(host="192.168.1.108")]) as r:
        targets = [("127.0.0.1", r.port)] * 8 + [("8.8.8.8", 37810)]
        t0 = time.monotonic()
        found = await dhip.search(0.3, targets=targets, broadcast=False)
        elapsed = time.monotonic() - t0
    assert [d.serial for d in found] == ["6J0123PAZ12345"]
    assert len(r.probe_times) == 8                         # 8.8.8.8 no se sondea (fuera de la LAN)
    # como mucho 5 paquetes en cualquier segundo: los 8 envíos ocupan más de 1 s
    assert elapsed >= 1.0
    for t in r.probe_times:
        assert len([x for x in r.probe_times if 0 <= x - t < 0.95]) <= 5


async def test_rate_limiter_and_lan_guard() -> None:
    lim = RateLimiter(5)
    t0 = time.monotonic()
    for _ in range(11):
        await lim.acquire()
    assert time.monotonic() - t0 >= 1.9 and lim.sent == 11
    assert is_lan_destination("192.168.1.10") and is_lan_destination("10.1.2.3") and is_lan_destination("127.0.0.1")
    assert is_lan_destination("255.255.255.255") and is_lan_destination("239.255.255.250")
    assert not is_lan_destination("8.8.8.8") and not is_lan_destination("nombre.ejemplo")


async def test_discover_merges_wsd_sadp_dhip_and_guesses_vendor() -> None:
    wsd_targets = [ProbeTarget("11111111-2222-3333-4444-555555555555", "http://192.168.1.70/onvif/device_service",
                               brand_scopes("UNV", "IPC2122LB-SF28-A")),
                   ProbeTarget("21111111-2222-3333-4444-555555555555", "http://192.168.1.64/onvif/device_service",
                               ["onvif://www.onvif.org/type/video_encoder"])]
    with WsDiscoveryResponder(wsd_targets) as w, SadpResponder([SadpTarget(host="192.168.1.64")]) as s, \
            DhipResponder([DhipTarget(host="192.168.1.108")]) as d:
        found = await discover(0.5, targets=[("127.0.0.1", w.port)], sadp_targets=[("127.0.0.1", s.port)],
                               dhip_targets=[("127.0.0.1", d.port)])
    by_host = {f.host: f for f in found}
    assert set(by_host) == {"192.168.1.64", "192.168.1.70", "192.168.1.108"}
    hik = by_host["192.168.1.64"]
    assert hik.vendor_guess == "hikvision" and hik.vendor_score > 0.7 and hik.sources == ["sadp", "wsd"]
    assert hik.serial and hik.mac == "c4:2f:90:f1:e6:a1" and hik.xaddrs
    assert by_host["192.168.1.108"].vendor_guess == "dahua" and by_host["192.168.1.108"].sources == ["dhip"]
    assert by_host["192.168.1.70"].vendor_guess == "uniview"


async def test_discover_with_wsd_targets_only_keeps_v1_behaviour() -> None:
    with WsDiscoveryResponder([ProbeTarget("31111111-2222-3333-4444-555555555555",
                                           "http://192.168.1.80/onvif/device_service",
                                           brand_scopes("VIVOTEK", "FD9389"))]) as w:
        found = await discover(0.4, targets=[("127.0.0.1", w.port)])
    assert [(f.host, f.vendor_guess, f.sources) for f in found] == [("192.168.1.80", "onvif", ["wsd"])]


@pytest.mark.parametrize("module", [sadp, dhip])
async def test_public_destinations_are_never_probed(module: object, caplog: pytest.LogCaptureFixture) -> None:
    found = await module.search(0.2, targets=[("1.1.1.1", 37020)], **({"multicast": False} if module is sadp  # type: ignore[attr-defined]
                                                                        else {"broadcast": False}))
    assert found == [] and "fuera de la red local" in caplog.text
    await asyncio.sleep(0)


# --------------------------------------------------------------------------- IP falsificada en el paquete
def test_discovery_uses_the_datagram_source_ip_not_the_declared_one() -> None:
    """Revisión B5: un paquete SADP/DHIP/WSD puede decir cualquier IP; manda la de origen del datagrama."""
    from tools.mocks.dhip import reply
    from tools.mocks.sadp import probe_match
    from vms.vendors.discovery import parse_probe_matches

    info = dhip.parse_reply(reply(DhipTarget(host="203.0.113.9")))
    assert dhip.to_device(info, "192.168.1.50").host == "192.168.1.50"
    assert dhip.to_device(info, "").host == "203.0.113.9"                 # sin origen: lo declarado
    assert dhip.to_device(info, "127.0.0.1").host == "203.0.113.9"        # respondedor local (pruebas)
    data = probe_match("U", SadpTarget(host="198.51.100.7"))
    assert sadp.parse_probe_match(data, "U", "192.168.1.51").host == "192.168.1.51"  # type: ignore[union-attr]
    wsd = (b'<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
           b'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><e:Body><d:ProbeMatches><d:ProbeMatch>'
           b"<d:Types>dn:NetworkVideoTransmitter</d:Types><d:XAddrs>http://203.0.113.9/onvif/device_service "
           b"http://192.168.1.52:8080/onvif/device_service</d:XAddrs></d:ProbeMatch></d:ProbeMatches>"
           b"</e:Body></e:Envelope>")
    found = parse_probe_matches(wsd, None, "192.168.1.52")
    assert found[0].host == "192.168.1.52" and found[0].http_port == 8080   # la XAddr que coincide con el origen
    assert parse_probe_matches(wsd, None, "192.168.1.53")[0].host == "192.168.1.53"


def test_lan_unicast_guard() -> None:
    from vms.vendors.netguard import is_lan_unicast
    assert is_lan_unicast("192.168.1.80") and is_lan_unicast("10.0.0.9") and is_lan_unicast("169.254.3.4")
    for bad in ("203.0.113.9", "8.8.8.8", "camara.example", "127.0.0.1", "239.255.255.250", "0.0.0.0", ""):
        assert not is_lan_unicast(bad), bad


def test_find_moves_ignores_devices_still_seen_at_their_ip_and_non_lan_hosts() -> None:
    """Revisión B5: un NVR que sigue en su IP y además anuncia otra (interfaz PoE) no se mueve."""
    from vms.core.interfaces import DiscoveredDevice
    from vms.core.models import Device
    from vms.vendors.ipwatch import find_moves

    serial = "DS-7608NI-K2SN0123456789"
    dev = Device.model_validate({"name": "NVR", "vendor": "hikvision", "host": "192.168.1.20", "follow_ip": True,
                                 "identity": {"serial": serial, "mac": "", "source": "api"}})
    both = [DiscoveredDevice(host="192.168.1.20", serial=serial, sources=["wsd"]),
            DiscoveredDevice(host="192.168.254.1", serial=serial, sources=["sadp"])]
    assert find_moves([dev], both) == []
    assert find_moves([dev], both[1:])[0].new_host == "192.168.254.1"
    public = [DiscoveredDevice(host="203.0.113.9", serial=serial, sources=["dhip"]),
              DiscoveredDevice(host="nvr.example", serial=serial, sources=["wsd"])]
    assert find_moves([dev], public) == []


def test_subnet_hint_explains_other_network() -> None:
    from vms.vendors.localnet import LocalNet, same_network, subnet_hint, suggested_pc_ip

    nets = [LocalNet("169.254.12.7", 16, "Ethernet")]
    assert not same_network("192.168.254.27", nets)
    hint = subnet_hint("192.168.254.27", nets)
    assert "192.168.254.250" in hint and "169.254.12.7" in hint
    assert subnet_hint("169.254.40.1", nets) == ""
    assert subnet_hint("192.168.254.27", [LocalNet("192.168.254.10", 24)]) == ""
    assert subnet_hint("192.168.254.27", []) == ""  # sin datos del PC no se avisa
    assert suggested_pc_ip("10.0.0.250") == "10.0.0.251"


def test_local_ipv4_skips_loopback_and_puts_link_local_last() -> None:
    from vms.vendors.localnet import local_ipv4

    nets = local_ipv4()
    assert all(not n.ip.startswith("127.") for n in nets)
    ll = [n.ip.startswith("169.254.") for n in nets]
    assert ll == sorted(ll)


async def test_sadp_search_by_interface_list() -> None:
    with SadpResponder([SadpTarget(host="192.168.254.27")]) as r:
        found = await sadp.search(0.4, targets=[("127.0.0.1", r.port)], multicast=False, interfaces=["127.0.0.1"])
    assert [d.host for d in found] == ["192.168.254.27"]
    assert r.probes_received == 2


async def test_scan_adds_network_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from vms.vendors import localnet

    monkeypatch.setattr(localnet, "local_ipv4", lambda: [localnet.LocalNet("169.254.3.4", 16)])
    assert "192.168.254.250" in localnet.subnet_hint("192.168.254.27", localnet.local_ipv4())
