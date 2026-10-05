"""Límites de red para el descubrimiento propio (SADP, DHIP): solo LAN y pocos paquetes por segundo.

DHIP (UDP 37810) se ha usado para ataques de amplificación: el programa nunca debe mandar sondas fuera de la
red local ni en ráfagas (PLAN-V2 §3.4: «limitado a la LAN y 5 paquetes/s»).
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import deque


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


__all__ = ["RateLimiter", "is_lan_destination"]
