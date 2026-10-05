"""Cambio de IP por DHCP (PLAN-V2 §3.2 punto 11): encontrar un equipo por su serie o MAC en otra IP.

Si el descubrimiento (WSD, SADP, DHIP) ve **la misma serie o MAC** de un equipo dado de alta en una IP distinta,
se propone «La cámara X ahora está en 192.168.1.80: ¿actualizar?».

El descubrimiento **no está autenticado**: cualquiera en la LAN puede contestar con la serie de otro equipo
(SADP y DHIP la difunden). Por eso:
- la IP nueva es la de origen del datagrama (`netguard.reply_host`) y tiene que ser una IP privada o de enlace
  local (`is_lan_unicast`), nunca un nombre ni una IP de Internet;
- si el equipo sigue viéndose en su IP actual (equipo con varias interfaces), no se propone nada;
- antes de usar la contraseña guardada en la IP nueva se comprueba con la API **autenticada** que la serie o
  la MAC coinciden (`verify_at_new_host`): es una sola petición Digest (nunca Basic). Si el driver no tiene
  API, o el equipo tiene permitido Basic, o la comprobación falla, el cambio no se aplica solo y la interfaz
  pide volver a escribir la contraseña (misma regla que al editar la dirección a mano).
Solo se cambia sola si el equipo tiene `follow_ip=true`, no da vídeo y la comprobación sale bien.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from vms.core.errors import DeviceAuthFailed, DeviceError
from vms.core.interfaces import DeviceClient, DiscoveredDevice
from vms.core.models import Device

from .netguard import is_lan_unicast

log = logging.getLogger("vms.vendors.ipwatch")


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
    verified: bool = False              # la API autenticada en la IP nueva dio la misma serie/MAC
    needs_password: bool = False        # para aplicarla hay que volver a escribir la contraseña
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


def _match(serial: str, mac: str, f: DiscoveredDevice) -> Literal["serial", "mac"] | None:
    if serial and same_serial(serial, f.serial):
        return "serial"
    if mac and mac == norm_mac(f.mac):
        return "mac"
    return None


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
        # sigue respondiendo en su IP (NVR con interfaz PoE interna, equipo con dos IP): no se mueve
        if any(f.host.lower() == dev.host.lower() and _match(serial, mac, f) for f in found):
            continue
        for f in found:
            if f.host.lower() == dev.host.lower() or f.host.lower() in used_hosts:
                continue
            match = _match(serial, mac, f)
            if match is None:
                continue
            if not is_lan_unicast(f.host):
                log.warning("Se ignora %s para «%s»: no es una IP de la red local", f.host, dev.name)
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


def cannot_verify(moved: Device) -> str | None:
    """Motivo por el que no se puede comprobar la identidad sin volver a escribir la contraseña (sin red)."""
    from . import has_api
    from .registry import get_driver

    if not is_lan_unicast(moved.host):
        return f"{moved.host} no es una IP de la red local"
    if not has_api(get_driver(moved.vendor)):
        return "esta marca no permite comprobar por la API que sea el mismo equipo"
    if moved.allow_basic:
        return "el equipo tiene permitida la autenticación Basic (la contraseña viajaría sin cifrar)"
    return None


async def verify_at_new_host(moved: Device, password: str,
                             factory: Callable[[Device, str], DeviceClient]) -> str | None:
    """Comprueba con la API autenticada que el equipo de la IP nueva es el mismo. None = sí; si no, el motivo.

    `moved` es el equipo ya con la IP nueva (`apply_move`). Una sola petición con credenciales (VendorAuth funde
    el cliente si se rechaza); nunca con Basic."""
    reason = cannot_verify(moved)
    if reason is not None:
        return reason
    serial, mac = identity_of(moved)
    client = factory(moved, password)
    try:
        info = await client.probe()
    except DeviceAuthFailed:
        return f"la contraseña guardada no vale en {moved.host}: puede ser otro equipo"
    except DeviceError as exc:
        return f"no se pudo comprobar el equipo en {moved.host} ({exc.message})"
    except Exception as exc:  # noqa: BLE001 - la comprobación nunca tumba la búsqueda
        log.debug("Comprobación de identidad en %s: %s", moved.host, type(exc).__name__)
        return f"no se pudo comprobar el equipo en {moved.host}"
    finally:
        await client.aclose()
    if (serial and same_serial(serial, info.serial)) or (mac and mac == norm_mac(info.mac)):
        return None
    return f"el equipo de {moved.host} tiene otra serie o MAC: no es el mismo"


__all__ = ["IpChangeProposal", "apply_move", "cannot_verify", "find_moves", "identity_of", "norm_mac", "same_serial",
           "verify_at_new_host"]
