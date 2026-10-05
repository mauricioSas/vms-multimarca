"""Límites de red para el descubrimiento propio (SADP, DHIP): solo LAN y pocos paquetes por segundo.

DHIP (UDP 37810) se ha usado para ataques de amplificación: el programa nunca debe mandar sondas fuera de la
red local ni en ráfagas (PLAN-V2 §3.4: «limitado a la LAN y 5 paquetes/s»).
"""
from __future__ import annotations

import asyncio
import ipaddress
import time


def is_lan_destination(host: str) -> bool:
    """IP privada, de enlace local, de loopback (pruebas), multicast local o difusión. Nunca Internet."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and (ip == ipaddress.IPv4Address("255.255.255.255")
                                                  or ip in ipaddress.ip_network("239.255.0.0/16")):
        return True
    return ip.is_private or ip.is_link_local or ip.is_loopback


class RateLimiter:
    """Cubo de fichas: como mucho `rate` envíos por segundo (con ráfaga de `rate`)."""

    def __init__(self, rate: float = 5.0) -> None:
        self.rate = float(rate)
        self.tokens = float(rate)
        self.last = time.monotonic()
        self.sent = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self.sent += 1
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


__all__ = ["RateLimiter", "is_lan_destination"]
