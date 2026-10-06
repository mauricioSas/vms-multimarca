"""Descubrimiento de cámaras y NVR en la red local: WS-Discovery, SADP (Hikvision) y DHIP (Dahua).

`discover()` lanza las tres búsquedas a la vez y junta los resultados por IP; la marca la decide el
registro (`best_match`) con todas las pistas: scopes WSD, que respondiera a SADP o a DHIP, modelo y MAC.

WS-Discovery (implementación propia): envía un Probe SOAP (tipo dn:NetworkVideoTransmitter) por UDP multicast a
239.255.255.250:3702 y recoge los ProbeMatches durante `timeout` segundos. El fabricante se
deduce de los scopes (onvif://www.onvif.org/hardware/..., /name/...) y del modelo.

- `targets`: además (o en lugar) del multicast, envía el Probe a direcciones concretas.
  Lo usan las pruebas con tools.mocks.wsdiscovery y sirve para sondear una IP fuera del
  segmento local (p. ej. por la VPN, donde el multicast no llega).
- `interfaces`: IPs locales por las que salir (una por tarjeta de red). Vacío = la de
  por defecto del sistema. En Windows con varias tarjetas conviene indicarlas.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

from vms.core.interfaces import DetectionHints, DiscoveredDevice
from vms.core.models import Vendor

from .netguard import RateLimiter, reply_host

log = logging.getLogger("vms.vendors.discovery")

MULTICAST_ADDR = ("239.255.255.250", 3702)

PROBE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header><w:MessageID>{message_id}</w:MessageID>"
    "<w:To e:mustUnderstand=\"true\">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
    "<w:Action e:mustUnderstand=\"true\">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
    "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>")


def build_probe(message_id: str) -> bytes:
    return PROBE_TEMPLATE.format(message_id=message_id).encode("utf-8")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def hints_for(scopes: list[str], model: str = "", name: str = "", *, mac: str = "", sadp: bool = False,
              dhip: bool = False, manufacturer: str = "") -> DetectionHints:
    return DetectionHints(scopes=scopes, model=model, name=name, mac=mac, sadp=sadp, dhip=dhip,
                          manufacturer=manufacturer)


def guess_vendor(scopes: list[str], model: str = "", name: str = "") -> Vendor:
    """Marca más probable con el registro de drivers (PLAN-V2 §3.2 punto 4); «onvif» si no hay ninguna clara."""
    from .registry import guess_vendor_id

    return guess_vendor_id(hints_for(scopes, model, name))[0]


def parse_probe_matches(data: bytes, expected_relates_to: str | None = None,
                        source_ip: str = "") -> list[DiscoveredDevice]:
    """Convierte un ProbeMatches en dispositivos. Ignora respuestas a otros Probe.

    `source_ip`: IP de origen del datagrama. Se prefiere la XAddr con esa IP; si ninguna la tiene, el equipo
    se registra con la IP de origen (la del paquete es falsificable, ver `netguard.reply_host`)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        log.debug("Respuesta WS-Discovery no es XML válido; se ignora")
        return []
    relates = next((el.text or "" for el in root.iter() if _local(el.tag) == "RelatesTo"), "").strip()
    if expected_relates_to and relates and relates != expected_relates_to:
        return []
    out: list[DiscoveredDevice] = []
    for match in (el for el in root.iter() if _local(el.tag) == "ProbeMatch"):
        fields: dict[str, str] = {}
        for child in match.iter():
            name = _local(child.tag)
            if name in ("Types", "Scopes", "XAddrs", "Address") and child.text:
                fields[name] = child.text.strip()
        types = fields.get("Types", "")
        if types and "NetworkVideoTransmitter" not in types and "Device" not in types:
            continue
        xaddrs = [x for x in fields.get("XAddrs", "").split() if x.lower().startswith("http")]
        if not xaddrs:
            continue
        scopes = fields.get("Scopes", "").split()
        model = name = mac = ""
        for s in scopes:
            low = s.lower()
            value = unquote(s.rsplit("/", 1)[-1])
            if "/hardware/" in low:
                model = value
            elif "/name/" in low:
                name = value
            elif "/mac/" in low:
                mac = value
        # Preferimos una XAddr IPv4 (algunas cámaras anuncian también IPv6 link-local)
        xaddrs.sort(key=lambda x: ((urlsplit(x).hostname or "") != source_ip, ":" in (urlsplit(x).hostname or "")))
        parts = urlsplit(xaddrs[0])
        host = reply_host(parts.hostname or "", source_ip)
        if not host:
            continue
        port = parts.port or (443 if parts.scheme == "https" else 80)
        from .registry import guess_vendor_id

        vendor, score = guess_vendor_id(hints_for(scopes, model, name, mac=mac))
        out.append(DiscoveredDevice(host=host, http_port=port, vendor_guess=vendor, vendor_score=score,
                                    model=model, name=name, mac=mac.lower(), xaddrs=xaddrs, scopes=scopes,
                                    sources=["wsd"]))
    return out


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.found: list[DiscoveredDevice] = []

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        try:
            devices = parse_probe_matches(data, self.message_id, str(addr[0]))
        except Exception:  # noqa: BLE001 - un paquete raro no debe tumbar el escaneo
            log.exception("Error interpretando una respuesta WS-Discovery de %s", addr[0])
            return
        self.found.extend(devices)

    def error_received(self, exc: Exception) -> None:
        log.debug("Error UDP durante el descubrimiento: %s", exc)


def _make_socket(interface: str | None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
    bind_ip = "0.0.0.0"
    if interface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        bind_ip = interface
    sock.bind((bind_ip, 0))
    sock.setblocking(False)
    return sock


async def discover_wsd(timeout: float = 3.0, *, targets: list[tuple[str, int]] | None = None,
                   interfaces: list[str] | None = None, multicast: bool | None = None) -> list[DiscoveredDevice]:
    """Busca equipos ONVIF. Con `targets` y sin `multicast=True` solo sondea esos destinos."""
    timeout = max(0.2, min(float(timeout), 30.0))
    use_multicast = multicast if multicast is not None else not targets
    loop = asyncio.get_running_loop()
    message_id = f"uuid:{uuid.uuid4()}"
    probe = build_probe(message_id)
    transports: list[asyncio.DatagramTransport] = []
    collectors: list[_Collector] = []
    for iface in (interfaces or [None]):  # type: ignore[list-item]
        try:
            sock = _make_socket(iface)
        except OSError as exc:
            log.warning("No se pudo preparar el descubrimiento por la interfaz %s: %s", iface or "por defecto", exc)
            continue
        collector = _Collector(message_id)
        transport, _ = await loop.create_datagram_endpoint(lambda c=collector: c, sock=sock)  # type: ignore[misc]
        transports.append(transport)
        collectors.append(collector)
    if not transports:
        log.error("No se pudo abrir ningún socket UDP para el descubrimiento")
        return []
    try:
        destinations = ([MULTICAST_ADDR] if use_multicast else []) + list(targets or [])
        # WS-Discovery recomienda repetir el Probe (UDP no es fiable): 2 envíos separados.
        for attempt in range(2):
            for transport in transports:
                for dest in destinations:
                    try:
                        transport.sendto(probe, dest)
                    except OSError as exc:
                        log.warning("No se pudo enviar el Probe a %s:%s: %s", dest[0], dest[1], exc)
            if attempt == 0:
                await asyncio.sleep(min(0.3, timeout / 3))
        await asyncio.sleep(timeout)
    finally:
        for transport in transports:
            transport.close()
    unique: dict[str, DiscoveredDevice] = {}
    for c in collectors:
        for d in c.found:
            key = f"{d.host}:{d.http_port}"
            if key not in unique:
                unique[key] = d
    found = sorted(unique.values(), key=_host_key)
    log.debug("WS-Discovery: %d equipos", len(found))
    return found


def _host_key(d: DiscoveredDevice) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in d.host.split("."))


def _merge(found: dict[str, DiscoveredDevice], dev: DiscoveredDevice) -> None:
    key = dev.host.lower()
    cur = found.get(key)
    if cur is None:
        found[key] = dev
        return
    upd: dict[str, object] = {"sources": sorted(set(cur.sources) | set(dev.sources))}
    for f in ("model", "name", "mac", "serial", "firmware"):
        if not getattr(cur, f) and getattr(dev, f):
            upd[f] = getattr(dev, f)
    if not cur.xaddrs and dev.xaddrs:
        upd["xaddrs"], upd["http_port"] = dev.xaddrs, dev.http_port
    found[key] = cur.model_copy(update=upd)


async def discover(timeout: float = 3.0, *, targets: list[tuple[str, int]] | None = None,
                   interfaces: list[str] | None = None, multicast: bool | None = None,
                   sadp_targets: list[tuple[str, int]] | None = None,
                   dhip_targets: list[tuple[str, int]] | None = None) -> list[DiscoveredDevice]:
    """WS-Discovery + SADP + DHIP a la vez; resultado por IP con la marca decidida por el registro.

    Con `targets` (WSD) y sin `multicast=True` solo se sondean los destinos dados (pruebas, VPN): SADP y DHIP
    solo se usan entonces si se les pasan sus propios destinos."""
    from . import dhip, sadp
    from .registry import guess_vendor_id

    timeout = max(0.2, min(float(timeout), 30.0))
    lan = multicast if multicast is not None else not (targets or sadp_targets or dhip_targets)
    if lan and interfaces is None:
        # Todas las tarjetas: con el WiFi y un switch de cámaras a la vez, o con el PC en 169.254 (switch sin
        # router), la tarjeta «por defecto» no es la del cable de las cámaras.
        from .localnet import local_ipv4

        interfaces = [n.ip for n in local_ipv4()] or None

    async def run_wsd() -> list[DiscoveredDevice]:
        if not (lan or targets):
            return []
        return await discover_wsd(timeout, targets=targets, interfaces=interfaces, multicast=lan)

    async def run_sadp() -> list[sadp.SadpDevice]:
        if not (lan or sadp_targets):
            return []
        try:
            return await sadp.search(timeout, targets=sadp_targets, multicast=lan, interfaces=interfaces)
        except OSError as exc:
            log.warning("No se pudo buscar por SADP: %s", exc)
            return []

    async def run_dhip() -> list[dhip.DhipDevice]:
        if not (lan or dhip_targets):
            return []
        ifaces: list[str | None] = list(interfaces) if interfaces else [None]
        limiter = RateLimiter(dhip.MAX_PACKETS_PER_S)
        runs = await asyncio.gather(*(dhip.search(timeout, targets=dhip_targets if i == 0 else None, broadcast=lan,
                                                  interface=iface, limiter=limiter)
                                      for i, iface in enumerate(ifaces)))
        return [d for run in runs for d in run]

    wsd_found, sadp_found, dhip_found = await asyncio.gather(run_wsd(), run_sadp(), run_dhip())
    found: dict[str, DiscoveredDevice] = {}
    for d in wsd_found:
        _merge(found, d)
    for s in sadp_found:
        _merge(found, DiscoveredDevice(host=s.host, http_port=s.http_port, model=s.model, serial=s.serial,
                                       mac=s.mac, firmware=s.firmware, name=s.model, sources=["sadp"]))
    for h in dhip_found:
        _merge(found, DiscoveredDevice(host=h.host, http_port=h.http_port, model=h.model, serial=h.serial,
                                       mac=h.mac, firmware=h.firmware, name=h.vendor or h.model, sources=["dhip"]))
    out: list[DiscoveredDevice] = []
    for d in sorted(found.values(), key=_host_key):
        manufacturer = d.name if "dhip" in d.sources else ""
        vendor, score = guess_vendor_id(hints_for(d.scopes, d.model, d.name, mac=d.mac, sadp="sadp" in d.sources,
                                                  dhip="dhip" in d.sources, manufacturer=manufacturer))
        out.append(d.model_copy(update={"vendor_guess": vendor, "vendor_score": score}))
    log.info("Descubrimiento terminado: %d equipos (WSD %d, SADP %d, DHIP %d)", len(out), len(wsd_found),
             len(sadp_found), len(dhip_found))
    return out
