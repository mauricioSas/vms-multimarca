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
