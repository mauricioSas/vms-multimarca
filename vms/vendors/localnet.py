"""Redes IPv4 de este PC: por qué tarjetas buscar equipos y si un equipo encontrado está en la misma red.

SADP (Hikvision) y DHIP (Dahua) encuentran equipos aunque tengan una IP de otra red (van por multicast o
difusión en el mismo cable), pero luego el vídeo (RTSP/HTTP) solo llega si el PC y el equipo están en la
misma red. Es el caso típico de un switch sin router: el PC queda en 169.254.x.x y la cámara en su IP de
fábrica. `subnet_hint` explica qué IP poner al PC.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass

log = logging.getLogger("vms.vendors.localnet")

MAX_INTERFACES = 8


@dataclass(frozen=True)
class LocalNet:
    ip: str
    prefix: int
    name: str = ""

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(f"{self.ip}/{self.prefix}", strict=False)  # type: ignore[return-value]


def _prefix(netmask: str | None) -> int:
    try:
        return ipaddress.ip_network(f"0.0.0.0/{netmask}").prefixlen if netmask else 24
    except ValueError:
        return 24


def local_ipv4() -> list[LocalNet]:
    """IPv4 de las tarjetas encendidas, sin loopback; primero las que no son de enlace local (169.254)."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil es dependencia del extra [vms]
        return []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError) as exc:
        log.debug("No se pudieron leer las tarjetas de red: %s", exc)
        return []
    out: list[LocalNet] = []
    for name, entries in addrs.items():
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        for a in entries:
            if a.family != socket.AF_INET or not a.address:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_loopback or ip.is_unspecified:
                continue
            out.append(LocalNet(a.address, _prefix(a.netmask), name))
    out.sort(key=lambda n: ipaddress.ip_address(n.ip).is_link_local)
    return out[:MAX_INTERFACES]


def same_network(host: str, nets: list[LocalNet]) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # un nombre: no se puede saber, no se avisa
    return ip.is_loopback or any(ip.version == 4 and ip in n.network for n in nets)


def suggested_pc_ip(host: str) -> str:
    """IP libre probable para el PC en la red /24 del equipo (la .250, o la .251 si el equipo es la .250)."""
    parts = host.split(".")
    last = "251" if parts[-1] == "250" else "250"
    return ".".join(parts[:3] + [last])


def subnet_hint(host: str, nets: list[LocalNet]) -> str:
    """Texto para el usuario si el equipo está en otra red que el PC; vacío si está en la misma."""
    if not nets or same_network(host, nets):
        return ""
    pcs = ", ".join(n.ip for n in nets[:3])
    return (f"Este equipo tiene la IP {host}, pero este PC está en otra red ({pcs}): el vídeo no llegará. "
            f"Pon al PC una IP fija de la misma red, por ejemplo {suggested_pc_ip(host)} con máscara "
            "255.255.255.0, o cambia la IP de la cámara a la red del PC.")


__all__ = ["LocalNet", "local_ipv4", "same_network", "subnet_hint", "suggested_pc_ip"]
