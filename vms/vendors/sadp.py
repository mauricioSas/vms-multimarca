"""Búsqueda SADP de Hikvision (UDP 37020, multicast 239.255.255.250). Implementación propia, **solo búsqueda**.

SADP también sirve para activar equipos y reiniciar contraseñas: eso **no** se implementa ni se envía nunca.
Solo se manda el mensaje `inquiry` y se leen las respuestas `ProbeMatch` (modelo, serie, MAC, IP, puertos,
firmware). Formato tomado de capturas públicas de la herramienta SADP; **no verificado con hardware** (las
respuestas exactas se fijarán con fixtures reales).

Las respuestas llegan por multicast al puerto 37020 o directamente al puerto de origen de la sonda; se
escuchan las dos cosas. Si otro programa tiene el 37020 (SADPServer.exe de iVMS-4200 o HikCentral), se comparte
cuando Windows lo deja (`_listen_sockets`); si no, se avisa de quién lo tiene. Solo se envía a destinos de la LAN
y con un límite de paquetes por segundo.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import socket
import struct
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from . import scanreport, winnet
from .netguard import RateLimiter, is_lan_destination, reply_host
from .scanreport import ListenStatus

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


def _bind_shared(ip: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind((ip, port))
    except OSError:
        sock.close()
        raise
    return sock


def _join(sock: socket.socket, iface: str | None) -> bool:
    mreq = struct.pack("4s4s", socket.inet_aton(SADP_GROUP), socket.inet_aton(iface or "0.0.0.0"))
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        return True
    except OSError as exc:
        log.debug("SADP: no se pudo unir al grupo por %s: %s", iface or "la tarjeta por defecto", exc)
        return False


def _port_in_use(exc: OSError) -> bool:
    # Windows: WSAEACCES (10013, Python lo da como errno 13) o WSAEADDRINUSE (10048)
    return exc.errno in (errno.EACCES, errno.EADDRINUSE) or getattr(exc, "winerror", None) in (10013, 10048)


# Último problema de escucha registrado. La búsqueda de cambios de IP repite discover() cada 120 s mientras una
# cámara con «seguir IP» no da vídeo: el mismo aviso (mismo puerto, error y programa) va a DEBUG hasta que cambie.
_last_noted: tuple[object, ...] | None = None


def _note(level: int, key: tuple[object, ...], msg: str, *args: object) -> None:
    global _last_noted
    log.log(logging.DEBUG if key == _last_noted else level, msg, *args)
    _last_noted = key


def _listen_sockets(interfaces: list[str | None], port: int) -> tuple[list[socket.socket], ListenStatus]:
    """Sockets en `port` unidos al grupo multicast por cada tarjeta (las respuestas SADP suelen ir ahí).

    Primero uno en 0.0.0.0 con SO_REUSEADDR: Windows lo deja aunque otro programa tenga el puerto en la IP de una
    tarjeta (SADPServer.exe de iVMS-4200 o HikCentral escucha así). Si otro lo tiene en 0.0.0.0 sin compartirlo,
    ese bind da WSAEACCES pero Windows aún deja uno por IP de tarjeta: se prueba así. Con SO_EXCLUSIVEADDRUSE no
    hay forma. Las reglas son las del mismo usuario; entre cuentas distintas (el servicio del backend frente al de
    SADPServer) Windows puede ser más estricto, y por eso el fallo se anota con quién tiene el puerto.

    Ojo con el modo por tarjeta: el multicast llega a los dos programas, pero lo que se manda directo a
    <IP de la tarjeta>:`port` va al socket más concreto, el nuestro. Mientras dura la búsqueda (unos segundos), el
    otro programa no recibe esas respuestas directas a sus propias sondas. Solo pasa en este modo de reserva."""
    global _last_noted
    try:
        sock = _bind_shared("", port)
    except OSError as exc:
        first = exc
    else:
        if any([_join(sock, iface) for iface in interfaces]):
            sock.setblocking(False)
            _last_noted = None          # vuelve a funcionar: el próximo problema se avisa otra vez
            return [sock], ListenStatus(port, True, "all")
        sock.close()
        _note(logging.WARNING, ("join", port),
              "SADP: no se pudo unir al grupo %s por ninguna tarjeta: pueden faltar cámaras Hikvision", SADP_GROUP)
        return [], ListenStatus(port, False, error="no se pudo unir al grupo multicast por ninguna tarjeta")
    holders = winnet.udp_port_holders(port)     # antes de abrir los nuestros: solo los de otros
    socks: list[socket.socket] = []
    if sys.platform == "win32":     # en Linux un socket en una IP concreta no recibe el multicast
        for iface in interfaces:
            if not iface:
                continue
            try:
                s = _bind_shared(iface, port)
            except OSError as exc:
                log.debug("SADP: tampoco se pudo escuchar en %s:%d: %s", iface, port, exc)
                continue
            if _join(s, iface):
                s.setblocking(False)
                socks.append(s)
            else:
                s.close()
    who = ", ".join(f"{h.name or 'proceso desconocido'} (pid {h.pid}, {h.ip})" for h in holders) or "no se sabe"
    status = ListenStatus(port, bool(socks), "per_nic" if socks else "", _port_in_use(first), str(first), holders)
    key = (port, getattr(first, "winerror", None) or first.errno, tuple(sorted((h.name, h.ip) for h in holders)))
    if socks:
        # No se sabe por qué falló el bind en 0.0.0.0: otro programa en todas las IP o, entre cuentas distintas,
        # uno en la IP de una sola tarjeta. Se dice lo que se ve, sin suponerlo.
        _note(logging.INFO, ("per_nic", *key),
              "SADP: no se pudo escuchar el puerto UDP %d en todas las IP (%s; lo usa: %s); se escucha por cada "
              "tarjeta", port, first, who)
    else:
        _note(logging.WARNING, ("busy", *key),
              "SADP: no se puede escuchar en el puerto UDP %d (%s); lo tiene: %s. Pueden faltar cámaras Hikvision "
              "en la búsqueda", port, first, who)
    return socks, status


async def _listen(interfaces: list[str | None], port: int) -> tuple[list[socket.socket], ListenStatus]:
    """`_listen_sockets` fuera del bucle de eventos: si el bind falla, psutil repasa todas las conexiones del PC para
    ver quién tiene el puerto, y eso no debe parar el resto del backend. Si la búsqueda se cancela entre tanto, los
    sockets que se abran se cierran al acabar."""
    task = asyncio.ensure_future(asyncio.to_thread(_listen_sockets, interfaces, port))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_close_unused)
        raise


def _close_unused(task: asyncio.Future[tuple[list[socket.socket], ListenStatus]]) -> None:
    if not task.cancelled() and task.exception() is None:
        for sock in task.result()[0]:
            sock.close()


async def search(timeout: float = 2.0, *, targets: list[tuple[str, int]] | None = None,
                 multicast: bool = True, interface: str | None = None,
                 interfaces: list[str] | None = None, port: int | None = None) -> list[SadpDevice]:
    """Manda `inquiry` (multicast y/o a destinos concretos de la LAN) y devuelve lo que responde.

    `interfaces`: IPs locales por las que preguntar (una por tarjeta); vacío = la tarjeta por defecto.
    `port`: puerto del grupo multicast y de escucha (por defecto `SADP_PORT`, 37020; otro solo en pruebas, que
    nunca deben ocupar el real). Si hay un informe de búsqueda abierto (`scanreport.collecting`), anota en él si
    se pudo escuchar."""
    port = port or SADP_PORT
    loop = asyncio.get_running_loop()
    probe_uuid = str(uuid.uuid4()).upper()
    probe = build_inquiry(probe_uuid)
    limiter = RateLimiter(MAX_PACKETS_PER_S)
    collector = _Collector(probe_uuid)
    ifaces: list[str | None] = list(interfaces) if interfaces else [interface]
    transports: list[asyncio.DatagramTransport] = []
    senders: list[asyncio.DatagramTransport] = []
    try:
        for iface in ifaces:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 1))
                if iface:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
                sock.bind((iface or "0.0.0.0", 0))
                sock.setblocking(False)
            except OSError as exc:
                log.debug("SADP: no se pudo preparar la tarjeta %s: %s", iface or "por defecto", exc)
                continue
            tx, _ = await loop.create_datagram_endpoint(lambda: collector, sock=sock)
            transports.append(tx)
            senders.append(tx)
        if not senders:
            raise OSError("ninguna tarjeta de red disponible para SADP")
        if multicast:
            lsocks, status = await _listen(ifaces, port)
            scanreport.note_sadp_listen(status)
            while lsocks:
                try:
                    ltx, _ = await loop.create_datagram_endpoint(lambda: collector, sock=lsocks[0])
                except BaseException:
                    for lsock in lsocks:         # los que aún no tiene ningún transporte
                        lsock.close()
                    raise
                transports.append(ltx)
                lsocks.pop(0)
        dests = [(SADP_GROUP, port)] if multicast else []
        for d in targets or []:
            if is_lan_destination(d[0]):
                dests.append(d)
            else:
                log.warning("SADP: destino fuera de la red local ignorado: %s", d[0])
        for attempt in range(2):
            for tx in senders:
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
