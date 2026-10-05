"""Límites de red para el descubrimiento propio (SADP, DHIP): solo LAN y pocos paquetes por segundo.

DHIP (UDP 37810) se ha usado para ataques de amplificación: el programa nunca debe mandar sondas fuera de la
red local ni en ráfagas (PLAN-V2 §3.4: «limitado a la LAN y 5 paquetes/s»).
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import deque

# Redes de equipos de la instalación: RFC 1918, CGNAT/VPN (100.64/10), enlace local y ULA IPv6. No se usa
# `ip.is_private` porque también da True a las redes de documentación (192.0.2/24, 198.51.100/24, 203.0.113/24).
_LAN_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "fc00::/7", "fe80::/10"))


def _in_lan(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip.version == net.version and ip in net for net in _LAN_NETS)


def is_lan_destination(host: str) -> bool:
    """IP privada, de enlace local, de loopback (pruebas), multicast local o difusión. Nunca Internet."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and (ip == ipaddress.IPv4Address("255.255.255.255")
                                                  or ip in ipaddress.ip_network("239.255.0.0/16")):
        return True
    return _in_lan(ip) or ip.is_loopback


def is_lan_unicast(host: str) -> bool:
    """IP literal de un equipo de la LAN (privada o de enlace local): ni nombre, ni loopback, ni multicast,
    ni difusión, ni Internet. Es la condición para aceptar una IP nueva que llega por descubrimiento."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        return False
    return _in_lan(ip)


def reply_host(declared: str, source: str) -> str:
    """IP con la que se registra una respuesta de descubrimiento (SADP, DHIP, WSD).

    Un paquete UDP puede decir cualquier IP en su contenido: se usa la IP **de origen** del datagrama. La
    declarada solo se usa si no hay origen o si el origen es loopback (respondedores locales: simuladores y
    pruebas en el mismo equipo, donde un atacante ya tendría el equipo entero)."""
    src = (source or "").strip()
    if not src:
        return declared
    try:
        loop = ipaddress.ip_address(src.strip("[]")).is_loopback
    except ValueError:
        loop = False
    return (declared or src) if loop else src


class RateLimiter:
    """Ventana deslizante: como mucho `rate` envíos en cualquier segundo (sin ráfagas por encima)."""

    def __init__(self, rate: float = 5.0, window: float = 1.0) -> None:
        self.limit = max(1, int(rate))
        self.window = float(window)
        self.sent = 0
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.window:
                    self._times.popleft()
                if len(self._times) < self.limit:
                    self._times.append(now)
                    self.sent += 1
                    return
                await asyncio.sleep(self.window - (now - self._times[0]) + 0.001)


__all__ = ["RateLimiter", "is_lan_destination", "is_lan_unicast", "reply_host"]
