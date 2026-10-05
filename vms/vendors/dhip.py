"""Descubrimiento de equipos Dahua por UDP 37810 (`DHDiscover.search` con cabecera DHIP), sin credenciales.

Procedencia y licencia
----------------------
`build_probe()` y `parse_reply()` están **adaptados** de `custom_components/dahua/discovery.py` del proyecto
rroller/dahua (https://github.com/rroller/dahua), commit 7e107b23d453f3dcb4e7a8a89cd072e3482e0f6f
(4-oct-2026), publicado con licencia MIT:

    MIT License

    Copyright (c) 2020 Joakim Sørensen @ludeeus

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

Cambios respecto al original: tipado estricto, lista de campos ampliada con la MAC y la IP, búsqueda de
varios equipos a la vez (difusión en la LAN y destinos concretos) en lugar de una sola dirección, y los
límites de esta aplicación: **solo destinos de la red local** y **como mucho 5 paquetes por segundo**
(el puerto 37810 se ha usado para ataques de amplificación). Lo medido por el autor original (VTO2000A,
DHI-NVR5464) **no** se ha repetido aquí con hardware.

Trama: 32 bytes de cabecera (tamaño de cabecera 32 en little endian, la magia «DHIP» como bytes, 8 bytes de
sesión e id a cero, y dos veces la longitud del JSON en 64 bits little endian) seguidos del JSON. El JSON a
pelo no recibe respuesta. La respuesta trae el JSON tras una cabecera de longitud variable: se busca la
primera «{» en lugar de cortar en un desplazamiento fijo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
from dataclasses import dataclass, field
from typing import Any

from .netguard import RateLimiter, is_lan_destination, reply_host

log = logging.getLogger("vms.vendors.dhip")

DISCOVERY_PORT = 37810
BROADCAST = "255.255.255.255"
MAX_PACKETS_PER_S = 5.0

_DHIP_MAGIC = b"DHIP"
_HEADER_SIZE = 32
_PROBE_BODY = json.dumps({"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}}).encode("utf-8")

# Solo se leen estos campos: el resto de lo que mande el equipo no se guarda.
_WANTED = ("SerialNo", "DeviceType", "DeviceClass", "Vendor", "Manufacturer", "Version", "HttpPort", "Port",
           "MachineName", "VideoInputChannels", "RemoteVideoInputChannels", "Mac", "IPv4Address")


def build_probe() -> bytes:
    """Los bytes que hacen responder a un Dahua en el 37810 (cabecera DHIP de 32 bytes + JSON)."""
    size = len(_PROBE_BODY)
    header = struct.pack("<I", _HEADER_SIZE) + _DHIP_MAGIC + bytes(8) + struct.pack("<QQ", size, size)
    return header + _PROBE_BODY


def parse_reply(raw: bytes) -> dict[str, Any]:
    """La descripción que el equipo da de sí mismo, o {} si no es una respuesta DHDiscover."""
    if not raw:
        return {}
    start = raw.find(b"{")
    if start < 0:
        return {}
    try:
        message = json.loads(raw[start:].decode("utf-8", "replace").rstrip("\x00"))
    except ValueError:
        return {}
    if not isinstance(message, dict):
        return {}
    params = message.get("params")
    info = params.get("deviceInfo") if isinstance(params, dict) else None
    if not isinstance(info, dict):
        return {}
    found = {key: info[key] for key in _WANTED if key in info}
    if not found.get("SerialNo") and not found.get("DeviceType"):
        return {}
    return found


@dataclass
class DhipDevice:
    host: str
    model: str = ""
    serial: str = ""
    mac: str = ""
    http_port: int = 80
    firmware: str = ""
    device_class: str = ""
    vendor: str = ""
    channels: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def to_device(info: dict[str, Any], source_ip: str) -> DhipDevice:
    ipv4 = info.get("IPv4Address")
    declared = str(ipv4.get("IPAddress") or "") if isinstance(ipv4, dict) else ""
    # La IP de origen del datagrama manda sobre la que dice el paquete (falsificable): ver netguard.reply_host
    host = reply_host(declared, source_ip)

    def num(key: str) -> int:
        v = info.get(key)
        return int(v) if isinstance(v, int) or (isinstance(v, str) and v.isdigit()) else 0

    return DhipDevice(host=str(host), model=str(info.get("DeviceType") or ""), serial=str(info.get("SerialNo") or ""),
                      mac=str(info.get("Mac") or "").lower(), http_port=num("HttpPort") or 80,
                      firmware=str(info.get("Version") or ""), device_class=str(info.get("DeviceClass") or ""),
                      vendor=str(info.get("Vendor") or info.get("Manufacturer") or ""),
                      channels=num("VideoInputChannels") + num("RemoteVideoInputChannels"), raw=info)


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, DhipDevice] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        info = parse_reply(data)
        if info:
            dev = to_device(info, str(addr[0]))
            self.found.setdefault(dev.serial or dev.host, dev)

    def error_received(self, exc: Exception) -> None:
        # Un ICMP «puerto inalcanzable» llega aquí: significa «sin servicio de descubrimiento», no es un fallo
        log.debug("Error UDP en DHIP: %s", exc)


async def search(timeout: float = 2.0, *, targets: list[tuple[str, int]] | None = None, broadcast: bool = True,
                 interface: str | None = None, limiter: RateLimiter | None = None) -> list[DhipDevice]:
    """Pregunta a la LAN (difusión) y/o a direcciones concretas. Nunca lanza: lo que no responde no cuesta nada."""
    loop = asyncio.get_running_loop()
    limiter = limiter or RateLimiter(MAX_PACKETS_PER_S)
    collector = _Collector()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((interface or "0.0.0.0", 0))
        sock.setblocking(False)
    except OSError as exc:
        log.warning("No se pudo preparar el descubrimiento DHIP: %s", exc)
        return []
    transport, _ = await loop.create_datagram_endpoint(lambda: collector, sock=sock)
    probe = build_probe()
    try:
        dests = [(BROADCAST, DISCOVERY_PORT)] if broadcast else []
        for d in targets or []:
            if is_lan_destination(d[0]):
                dests.append(d)
            else:
                log.warning("DHIP: destino fuera de la red local ignorado: %s", d[0])
        for dest in dests:
            await limiter.acquire()
            try:
                transport.sendto(probe, dest)
            except OSError as exc:
                log.debug("No se pudo enviar DHIP a %s:%s: %s", dest[0], dest[1], exc)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return list(collector.found.values())


__all__ = ["BROADCAST", "DISCOVERY_PORT", "DhipDevice", "MAX_PACKETS_PER_S", "build_probe", "parse_reply", "search",
           "to_device"]
