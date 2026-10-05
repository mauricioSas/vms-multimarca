"""Búsqueda SADP de Hikvision (UDP 37020, multicast 239.255.255.250). Implementación propia, **solo búsqueda**.

SADP también sirve para activar equipos y reiniciar contraseñas: eso **no** se implementa ni se envía nunca.
Solo se manda el mensaje `inquiry` y se leen las respuestas `ProbeMatch` (modelo, serie, MAC, IP, puertos,
firmware). Formato tomado de capturas públicas de la herramienta SADP; **no verificado con hardware** (las
respuestas exactas se fijarán con fixtures reales).

Las respuestas llegan por multicast al puerto 37020 o directamente al puerto de origen de la sonda; se
escuchan las dos cosas. Solo se envía a destinos de la LAN y con un límite de paquetes por segundo.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from .netguard import RateLimiter, is_lan_destination, reply_host

log = logging.getLogger("vms.vendors.sadp")

SADP_PORT = 37020
SADP_GROUP = "239.255.255.250"
MAX_PACKETS_PER_S = 5.0


def build_inquiry(probe_uuid: str) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{probe_uuid}</Uuid>'
            "<Types>inquiry</Types></Probe>").encode("utf-8")


@dataclass
class SadpDevice:
    host: str
    model: str = ""
    serial: str = ""
    mac: str = ""
    http_port: int = 80
    command_port: int = 8000
    firmware: str = ""
    device_type: str = ""
    activated: bool | None = None
    analog_channels: int = 0
    digital_channels: int = 0
    raw: dict[str, str] = field(default_factory=dict)


def _norm_mac(mac: str) -> str:
    hexes = "".join(c for c in mac.lower() if c in "0123456789abcdef")
    return ":".join(hexes[i:i + 2] for i in range(0, len(hexes), 2)) if len(hexes) == 12 else mac.lower()


def parse_probe_match(data: bytes, expected_uuid: str | None = None, source_ip: str = "") -> SadpDevice | None:
    """Una respuesta `ProbeMatch` → SadpDevice; None si no es una respuesta SADP (o es a otra sonda).

    `source_ip`: IP de origen del datagrama; manda sobre la que el paquete dice tener (ver `reply_host`)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if root.tag != "ProbeMatch":
        return None
    raw = {child.tag: (child.text or "").strip() for child in root}
    if expected_uuid and raw.get("Uuid") and raw["Uuid"].upper() != expected_uuid.upper():
        return None
    declared = raw.get("IPv4Address", "")
    host = reply_host(declared, source_ip)
    if not host:
        return None
    if declared and host != declared:
        log.debug("SADP: la respuesta de %s dice ser %s; se usa la IP de origen", host, declared)

    def num(key: str, default: int = 0) -> int:
        v = raw.get(key, "")
        return int(v) if v.isdigit() else default

    act = raw.get("Activated", "").lower()
    return SadpDevice(host=host, model=raw.get("DeviceDescription", ""), serial=raw.get("DeviceSN", ""),
                      mac=_norm_mac(raw.get("MAC", "")), http_port=num("HttpPort", 80),
                      command_port=num("CommandPort", 8000), firmware=raw.get("SoftwareVersion", ""),
                      device_type=raw.get("DeviceType", ""),
                      activated=True if act == "true" else False if act == "false" else None,
                      analog_channels=num("AnalogChannelNum"), digital_channels=num("DigitalChannelNum"), raw=raw)


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, probe_uuid: str) -> None:
        self.probe_uuid = probe_uuid
        self.found: dict[str, SadpDevice] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        try:
            dev = parse_probe_match(data, self.probe_uuid, str(addr[0]))
        except Exception:  # noqa: BLE001 - un paquete raro no tumba la búsqueda
            log.exception("Respuesta SADP ilegible")
            return
        if dev is not None:
            self.found.setdefault(dev.serial or dev.host, dev)

    def error_received(self, exc: Exception) -> None:
        log.debug("Error UDP en SADP: %s", exc)


def _listen_socket(interface: str | None) -> socket.socket | None:
    """Socket unido al grupo multicast en el puerto 37020 (las respuestas SADP suelen ir ahí)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", SADP_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(SADP_GROUP), socket.inet_aton(interface or "0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        return sock
    except OSError as exc:
        log.debug("No se pudo escuchar SADP en el puerto %d: %s", SADP_PORT, exc)
        return None


async def search(timeout: float = 2.0, *, targets: list[tuple[str, int]] | None = None,
                 multicast: bool = True, interface: str | None = None) -> list[SadpDevice]:
    """Manda `inquiry` (multicast y/o a destinos concretos de la LAN) y devuelve lo que responde."""
    loop = asyncio.get_running_loop()
    probe_uuid = str(uuid.uuid4()).upper()
    probe = build_inquiry(probe_uuid)
    limiter = RateLimiter(MAX_PACKETS_PER_S)
    collector = _Collector(probe_uuid)
    transports: list[asyncio.DatagramTransport] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 1))
    if interface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
    sock.bind((interface or "0.0.0.0", 0))
    sock.setblocking(False)
    tx, _ = await loop.create_datagram_endpoint(lambda: collector, sock=sock)
    transports.append(tx)
    if multicast:
        lsock = _listen_socket(interface)
        if lsock is not None:
            ltx, _ = await loop.create_datagram_endpoint(lambda: collector, sock=lsock)
            transports.append(ltx)
    try:
        dests = [(SADP_GROUP, SADP_PORT)] if multicast else []
        for d in targets or []:
            if is_lan_destination(d[0]):
                dests.append(d)
            else:
                log.warning("SADP: destino fuera de la red local ignorado: %s", d[0])
        for attempt in range(2):
            for dest in dests:
                await limiter.acquire()
                try:
                    tx.sendto(probe, dest)
                except OSError as exc:
                    log.debug("No se pudo enviar SADP a %s:%s: %s", dest[0], dest[1], exc)
            if attempt == 0:
                await asyncio.sleep(min(0.3, timeout / 3))
        await asyncio.sleep(timeout)
    finally:
        for t in transports:
            t.close()
    return list(collector.found.values())


__all__ = ["MAX_PACKETS_PER_S", "SADP_GROUP", "SADP_PORT", "SadpDevice", "build_inquiry", "parse_probe_match",
           "search"]
