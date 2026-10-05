"""Cambio de IP por DHCP (PLAN-V2 §3.2 punto 11): encontrar un equipo por su serie o MAC en otra IP.

Si el descubrimiento (WSD, SADP, DHIP) ve **la misma serie o MAC** de un equipo dado de alta en una IP distinta,
se propone «La cámara X ahora está en 192.168.1.80: ¿actualizar?». Solo se cambia sola si el equipo tiene
`follow_ip=true` (lo activa el administrador). La contraseña guardada se reutiliza porque la identidad del
equipo (serie/MAC leídas sin credenciales) coincide: es el mismo equipo, no otro en esa dirección.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from vms.core.interfaces import DiscoveredDevice
from vms.core.models import Device


class IpChangeProposal(BaseModel):
    device_id: str
    device_name: str
    old_host: str
    new_host: str
    http_port: int = 80
    match: Literal["serial", "mac"]
    sources: list[str] = []
    follow_ip: bool = False
    applied: bool = False
    message_es: str = ""


def norm_mac(mac: str) -> str:
    hexes = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2)) if len(hexes) == 12 else ""


def norm_serial(serial: str) -> str:
    return re.sub(r"\s+", "", serial or "").upper()


def same_serial(a: str, b: str) -> bool:
    """Igual, o una termina en la otra con al menos 9 caracteres (ONVIF a veces da la serie corta)."""
    a, b = norm_serial(a), norm_serial(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 9 and long_.endswith(short)


def identity_of(dev: Device) -> tuple[str, str]:
    serial = (dev.identity.serial if dev.identity and dev.identity.serial else "") or dev.serial
    mac = dev.identity.mac if dev.identity and dev.identity.mac else ""
    return serial, norm_mac(mac)


def find_moves(devices: list[Device], found: list[DiscoveredDevice], *,
               only: set[str] | None = None) -> list[IpChangeProposal]:
    """Propuestas de cambio de IP. `only`: ids de equipos que han dejado de responder (None = todos)."""
    used_hosts = {d.host.lower() for d in devices}
    out: list[IpChangeProposal] = []
    for dev in devices:
        if only is not None and dev.id not in only:
            continue
        serial, mac = identity_of(dev)
        if not serial and not mac:
            continue
        for f in found:
            if f.host.lower() == dev.host.lower() or f.host.lower() in used_hosts:
                continue
            match: Literal["serial", "mac"] | None = None
            if serial and same_serial(serial, f.serial):
                match = "serial"
            elif mac and mac == norm_mac(f.mac):
                match = "mac"
            if match is None:
                continue
            out.append(IpChangeProposal(
                device_id=dev.id, device_name=dev.name, old_host=dev.host, new_host=f.host, http_port=f.http_port,
                match=match, sources=list(f.sources), follow_ip=dev.follow_ip,
                message_es=f"«{dev.name}» ahora está en {f.host} (antes {dev.host}): ¿actualizar?"))
            break
    return out


def apply_move(dev: Device, proposal: IpChangeProposal, now: datetime) -> Device:
    """Equipo con la IP nueva (la identidad no cambia; se anota cuándo se vio)."""
    data = dev.model_dump()
    data.update(host=proposal.new_host, updated_at=now)
    if data.get("identity"):
        data["identity"]["seen_at"] = now
    return Device.model_validate(data)


__all__ = ["IpChangeProposal", "apply_move", "find_moves", "identity_of", "norm_mac", "same_serial"]
