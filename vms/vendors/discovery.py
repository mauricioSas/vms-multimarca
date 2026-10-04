"""Descubrimiento de cámaras y NVR en la red local con WS-Discovery (implementación propia).

Envía un Probe SOAP (tipo dn:NetworkVideoTransmitter) por UDP multicast a
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
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

from vms.core.interfaces import DiscoveredDevice
from vms.core.models import Vendor

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


def guess_vendor(scopes: list[str], model: str = "", name: str = "") -> Vendor:
    text = " ".join([*scopes, model, name]).upper()
    if "HIKVISION" in text or "/DS-" in text or model.upper().startswith(("DS-", "IDS-")):
        return "hikvision"
    if "DAHUA" in text or model.upper().startswith(("DH-", "DHI-", "IPC-", "NVR4", "NVR5", "XVR", "HCVR")):
        return "dahua"
    return "onvif"


def parse_probe_matches(data: bytes, expected_relates_to: str | None = None) -> list[DiscoveredDevice]:
    """Convierte un ProbeMatches en dispositivos. Ignora respuestas a otros Probe."""
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
        xaddrs.sort(key=lambda x: ":" in (urlsplit(x).hostname or ""))
        parts = urlsplit(xaddrs[0])
        host = parts.hostname or ""
        if not host:
            continue
        port = parts.port or (443 if parts.scheme == "https" else 80)
        out.append(DiscoveredDevice(host=host, http_port=port, vendor_guess=guess_vendor(scopes, model, name),
                                    model=model, name=name, mac=mac, xaddrs=xaddrs, scopes=scopes))
    return out


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.found: list[DiscoveredDevice] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
        try:
            devices = parse_probe_matches(data, self.message_id)
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


async def discover(timeout: float = 3.0, *, targets: list[tuple[str, int]] | None = None,
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
    found = sorted(unique.values(), key=lambda d: tuple(int(p) if p.isdigit() else 0 for p in d.host.split(".")))
    log.info("Descubrimiento terminado: %d equipos encontrados", len(found))
    return found
