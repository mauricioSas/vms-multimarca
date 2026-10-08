"""Lo que Windows sabe de la red y explica una búsqueda vacía: el perfil de cada tarjeta y qué programa tiene un
puerto UDP. Solo se lee; nunca se cambia nada.

- Perfil de red («Pública», «Privada», «Dominio»): Windows pone «Pública» a una red sin router («Red no
  identificada», lo típico con un switch solo para cámaras) y las reglas del firewall del producto nunca se abren
  en ese perfil (`vmsctl firewall`). En una red pública el firewall puede descartar las respuestas de las cámaras
  sin avisar. Se lee la clase CIM `MSFT_NetConnectionProfile` (lo mismo que `Get-NetConnectionProfile`) con
  PowerShell: la categoría llega como número, así que no depende del idioma de Windows. Solo tienen perfil las
  tarjetas conectadas. Límite de 5 s y **sin guardar nada** entre búsquedas: el aviso le dice al usuario que la
  ponga como «Privada» y vuelva a buscar, y esa búsqueda tiene que ver el cambio (y no repetir un fallo
  anterior). Sin datos (otro sistema, error, la cuenta del servicio no puede leerlo) devuelve {}: «no se sabe»,
  nunca un error.
- Puerto ocupado: `psutil.net_connections` dice qué proceso escucha en un puerto UDP, p. ej. SADPServer.exe (la
  búsqueda SADP de iVMS-4200 o HikCentral) en el 37020.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("vms.vendors.winnet")

Category = Literal["public", "private", "domain"]
# NetConnectionProfile.NetworkCategory (número; con texto, por si PowerShell lo da así)
_CATEGORIES: dict[object, Category] = {0: "public", 1: "private", 2: "domain", "public": "public",
                                        "private": "private", "domainauthenticated": "domain"}
# NetConnectionProfile.IPv4Connectivity: 0 desconectada … 3 red local, 4 Internet (o su texto, como la categoría)
_IPV4_INTERNET: tuple[object, ...] = (4, "internet")
PROFILE_TIMEOUT_S = 5.0

# Salida en UTF-8 (los nombres de tarjeta llevan tildes: «Conexión de área local») y siempre una lista JSON. Sin
# barra de progreso: con la salida redirigida, PowerShell 5.1 la manda en CLIXML a la salida de error.
_PS_PROFILES = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
                "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding $false;"
                "ConvertTo-Json -Compress -InputObject @(Get-CimInstance -Namespace root/StandardCimv2 "
                "-ClassName MSFT_NetConnectionProfile | "
                "Select-Object InterfaceAlias,InterfaceIndex,NetworkCategory,IPv4Connectivity,Name)")


@dataclass(frozen=True)
class NetProfile:
    alias: str              # nombre de la tarjeta en Windows («Ethernet», «Wi-Fi»): el mismo que da psutil
    index: int
    category: Category
    internet: bool          # Windows ve Internet por esa tarjeta
    network: str = ""       # nombre de la red («Red no identificada», el de la wifi…)


@dataclass(frozen=True)
class PortHolder:
    pid: int | None
    name: str               # «SADPServer.exe»; vacío si Windows no deja verlo
    ip: str = ""            # IP local en la que escucha («0.0.0.0» = todas)


def parse_profiles(text: str) -> dict[str, NetProfile]:
    """Salida de `_PS_PROFILES` → {alias: NetProfile}. Acepta un objeto suelto o una lista (PowerShell 5.1 no
    envuelve en lista un solo elemento si se usa la tubería) y descarta lo que no entienda. Una tarjeta puede tener
    varios perfiles (una red IPv4 y otra IPv6): si alguno es «Pública», se queda ese, que es el que bloquea."""
    try:
        data = json.loads(text.lstrip("\ufeff").strip() or "[]")
    except ValueError:
        return {}
    out: dict[str, NetProfile] = {}
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict):
            continue
        alias, raw = item.get("InterfaceAlias"), item.get("NetworkCategory")
        key = raw.lower() if isinstance(raw, str) else raw if type(raw) is int else None
        category = _CATEGORIES.get(key) if key is not None else None
        if not isinstance(alias, str) or not alias or category is None:
            continue
        if alias in out and out[alias].category == "public":
            continue
        index = item.get("InterfaceIndex")
        name = item.get("Name")
        conn = item.get("IPv4Connectivity")
        conn = conn.lower() if isinstance(conn, str) else conn if type(conn) is int else None
        out[alias] = NetProfile(alias=alias, index=index if type(index) is int else 0, category=category,
                                internet=conn in _IPV4_INTERNET, network=name if isinstance(name, str) else "")
    return out


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


_CLIXML_ERROR = re.compile(r'<S S="Error">(.*?)</S>', re.S)


def _ps_error(stderr: bytes) -> str:
    """Primera línea útil de la salida de error de PowerShell. Con la salida redirigida, PowerShell 5.1 la da a
    veces en CLIXML («#< CLIXML» y una línea <Objs…>): entonces se toma el texto de sus <S S="Error">."""
    text = stderr.decode("utf-8", errors="replace")
    errors = _CLIXML_ERROR.findall(text)
    if errors:
        text = html.unescape("".join(errors)).replace("_x000D__x000A_", "\n").replace("_x000A_", "\n")
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#< CLIXML", "<Objs")):
            return line
    return "sin detalle"


def _query_profiles() -> str:
    if sys.platform != "win32":
        raise OSError("el perfil de red solo existe en Windows")
    # stdin cerrado: bajo el servicio no hay consola y PowerShell no debe heredar nada que leer
    r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", _PS_PROFILES],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=PROFILE_TIMEOUT_S, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise OSError(f"PowerShell terminó con el código {r.returncode}: {_ps_error(r.stderr)}")
    return r.stdout.decode("utf-8", errors="replace")


def network_profiles() -> dict[str, NetProfile]:
    """Perfil de red de cada tarjeta conectada, por alias; {} si no se puede saber. Lo lee cada vez (no hay
    caché: ver arriba) y bloquea hasta 5 s: llámala con `asyncio.to_thread`. Solo la usa la búsqueda que lanza el
    usuario, nunca la periódica de cambios de IP."""
    try:
        return parse_profiles(_query_profiles())
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("No se pudo saber el perfil de red de las tarjetas: %s", exc)
        return {}


def udp_port_holders(port: int) -> list[PortHolder]:
    """Procesos con un socket UDP IPv4 en `port` (en cualquier IP local); [] si no se puede saber."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil es dependencia del extra [vms]
        return []
    try:
        conns = psutil.net_connections(kind="udp4")
    except (OSError, psutil.Error) as exc:
        log.debug("No se pudo ver quién usa el puerto UDP %d: %s", port, exc)
        return []
    out: dict[tuple[int | None, str], PortHolder] = {}
    for c in conns:
        if not c.laddr or c.laddr.port != port:
            continue
        name = ""
        if c.pid:
            try:
                name = psutil.Process(c.pid).name()
            except (OSError, psutil.Error):
                name = ""
        out.setdefault((c.pid, c.laddr.ip), PortHolder(c.pid, name, c.laddr.ip))
    return list(out.values())


__all__ = ["NetProfile", "PortHolder", "network_profiles", "parse_profiles", "udp_port_holders"]
