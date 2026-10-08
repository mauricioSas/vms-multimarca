"""Informe de una búsqueda en la red, para explicar al usuario por qué no aparece una cámara.

`discovery_scan` (API) abre un informe con `collecting()`. Mientras dura, `discover()` anota por qué tarjetas
buscó y su perfil de red en Windows, y `sadp.search()` si pudo escuchar en el puerto de SADP. Va por contextvars:
cada petición tiene su informe, la búsqueda periódica de cambios de IP no abre ninguno y el `Discoverer` de la API
sigue devolviendo solo la lista de equipos (las pruebas lo sustituyen por dobles).

`explain()` convierte el informe en avisos en español con «Qué hacer:». Nunca cambia nada del sistema: el perfil
de red lo cambia el usuario si quiere (una línea de PowerShell como administrador) y el firewall no se abre nunca
en el perfil Público.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from vms.core.interfaces import DiscoveredDevice

from .winnet import NetProfile, PortHolder

MAX_PUBLIC_WARNINGS = 3


@dataclass
class ListenStatus:
    """Resultado de escuchar las respuestas SADP que llegan por multicast a su puerto."""
    port: int
    ok: bool
    mode: Literal["all", "per_nic", ""] = ""   # "all": en 0.0.0.0; "per_nic": un socket por IP de tarjeta
    in_use: bool = False                       # el puerto lo tiene otro programa (WSAEACCES / WSAEADDRINUSE)
    error: str = ""
    holders: list[PortHolder] = field(default_factory=list)


@dataclass
class NicInfo:
    ip: str
    name: str
    category: str = ""          # "public" | "private" | "domain" | "" (no se sabe)
    network: str = ""           # nombre de la red en Windows («Red no identificada»…)
    internet: bool | None = None
    index: int = 0


@dataclass
class ScanWarning:
    code: Literal["public_network", "sadp_port_busy", "sadp_listen_failed"]
    text_es: str                # con «Qué hacer:»
    command: str = ""           # línea para PowerShell como administrador (la web la muestra tras el texto)


@dataclass
class ScanReport:
    nics: list[NicInfo] = field(default_factory=list)
    sadp_listen: ListenStatus | None = None

    def add_profiles(self, profiles: dict[str, NetProfile]) -> None:
        for nic in self.nics:
            p = profiles.get(nic.name)
            if p is not None:
                nic.category, nic.network, nic.internet, nic.index = p.category, p.network, p.internet, p.index

    def as_dict(self, found: Sequence[DiscoveredDevice]) -> dict[str, Any]:
        return {"nics": [asdict(n) for n in self.nics],
                "sadp_listen": asdict(self.sadp_listen) if self.sadp_listen is not None else None,
                "warnings": [asdict(w) for w in explain(self, found)]}


_CURRENT: ContextVar[ScanReport | None] = ContextVar("vms_scan_report", default=None)


@contextmanager
def collecting() -> Iterator[ScanReport]:
    """Abre un informe para la búsqueda que se haga dentro del bloque (en esta tarea y sus subtareas)."""
    report = ScanReport()
    token = _CURRENT.set(report)
    try:
        yield report
    finally:
        _CURRENT.reset(token)


def current() -> ScanReport | None:
    return _CURRENT.get()


def note_sadp_listen(status: ListenStatus) -> None:
    report = _CURRENT.get()
    if report is not None:
        report.sadp_listen = status


# --------------------------------------------------------------------------- avisos
_HIK_HINTS = ("sadp", "ivms", "hikcentral")
# Comodines y comillas: con ellos se usa el número de la tarjeta. PowerShell también toma ‘ ’ ‚ ‛ como comillas
# simples (y “ ” „ como dobles): un nombre con «’» cerraría la cadena de -InterfaceAlias.
_PS_SPECIAL = set("*?[]`'\"‘’‚‛“”„")
# Tarjetas virtuales (Hyper-V y WSL «vEthernet», VMware, VirtualBox, VPN): suelen ser «Red no identificada»,
# «Pública» y sin Internet, como el switch de las cámaras. Van al final para no dejar fuera, por el límite de
# avisos, la tarjeta de verdad.
_VIRTUAL_HINTS = ("vethernet", "hyper-v", "wsl", "vmware", "vmnet", "virtualbox", "docker", "vpn", "tap-",
                  "tailscale", "zerotier", "hamachi", "wireguard")
_WIRELESS_HINTS = ("wi-fi", "wifi", "wlan", "wireless", "inalámbric")


def _is_hikvision(h: PortHolder) -> bool:
    return any(k in h.name.lower() for k in _HIK_HINTS)


def _is_virtual(name: str) -> bool:
    return any(k in name.lower() for k in _VIRTUAL_HINTS)


def _is_wireless(name: str) -> bool:
    return any(k in name.lower() for k in _WIRELESS_HINTS)


def _holder_text(h: PortHolder) -> str:
    low = h.name.lower()
    if "sadp" in low:
        return f"{h.name} (la búsqueda SADP de iVMS-4200 o HikCentral, de Hikvision)"
    if "ivms" in low:
        return f"{h.name} (iVMS-4200, de Hikvision)"
    if "hikcentral" in low:
        return f"{h.name} (HikCentral, de Hikvision)"
    return h.name


def _sadp_warning(st: ListenStatus) -> ScanWarning:
    what = f"el puerto UDP {st.port} de este PC, el que usa la búsqueda de cámaras Hikvision (SADP)"
    missing = "Por eso pueden faltar cámaras Hikvision en la lista."
    if not st.in_use:
        return ScanWarning("sadp_listen_failed",
                           f"No se ha podido escuchar en {what}. {missing} Qué hacer: vuelve a buscar dentro de un "
                           "momento; si sigue igual, añade la cámara con su IP con «Añadir equipo».")
    named = [h for h in st.holders if h.name]
    if not named:
        return ScanWarning("sadp_port_busy",
                           f"Otro programa tiene ocupado {what}, y Windows no deja ver cuál. {missing} Qué hacer: "
                           "si tienes abiertos iVMS-4200, HikCentral o la herramienta SADP de Hikvision, ciérralos y "
                           "vuelve a buscar. Si no, añade la cámara con su IP con «Añadir equipo».")
    names = sorted({_holder_text(h) for h in named})
    if any(_is_hikvision(h) for h in named):
        todo = ("si no estás usando iVMS-4200 ni HikCentral, ciérralos y vuelve a buscar. Si no puedes cerrarlos "
                "(HikCentral lo deja como servicio de Windows), mira la IP de la cámara con su herramienta SADP y "
                "añádela aquí con «Añadir equipo».")
    else:
        todo = ("cierra ese programa y vuelve a buscar. Si no puedes cerrarlo, añade la cámara con su IP con "
                "«Añadir equipo».")
    return ScanWarning("sadp_port_busy",
                       f"Otro programa tiene ocupado {what}: {_join(names)}. {missing} Qué hacer: {todo}")


def _private_command(nic: NicInfo) -> str:
    if nic.name and not _PS_SPECIAL.intersection(nic.name):
        return f"Set-NetConnectionProfile -InterfaceAlias '{nic.name}' -NetworkCategory Private"
    if nic.index > 0:
        return f"Set-NetConnectionProfile -InterfaceIndex {nic.index} -NetworkCategory Private"
    return ""


def _public_warning(nics: Sequence[NicInfo]) -> ScanWarning:
    """Un aviso por tarjeta, aunque tenga varias IP (la suya y otra añadida para llegar a una cámara con la IP de
    fábrica, o una 169.254): el perfil es de la tarjeta y la línea para cambiarlo, la misma."""
    nic = nics[0]
    where = f"«{nic.name}» ({', '.join(n.ip for n in nics)}{', ' + nic.network if nic.network else ''})"
    wireless = _is_wireless(nic.name)
    # Una wifi con portal de acceso (la de un bar o un hotel antes de aceptar) tampoco tiene Internet: con wifi,
    # la advertencia sale siempre.
    caution = ("" if nic.internet is False and not wireless else
               " (nunca si es una wifi pública, como la de un bar o un hotel)")
    # Sin router («Red no identificada») Windows no recuerda la red y al reiniciar puede volver a «Pública». Una
    # wifi la recuerda por su nombre.
    again = (" Con un switch sin router, Windows puede volver a ponerla como «Pública» al reiniciar el PC; si pasa, "
             "repite este paso." if nic.internet is not True and not wireless else "")
    command = _private_command(nic)
    how = ("abre PowerShell como administrador (botón derecho en el botón Inicio → «Terminal (administrador)» o "
           "«Windows PowerShell (administrador)»), pega esta línea y vuelve a buscar:" if command else
           "ve a Configuración → Red e Internet → esa red → Tipo de perfil de red, y vuelve a buscar.")
    return ScanWarning("public_network",
                       f"Windows tiene como «Pública» la red de la conexión {where} de este PC, y en una red pública "
                       "el firewall de Windows puede bloquear las respuestas de las cámaras. Qué hacer: si las "
                       f"cámaras van conectadas por ahí, pon esa red como «Privada»{caution}.{again} Para ponerla "
                       f"como «Privada», {how}", command)


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def explain(report: ScanReport, found: Sequence[DiscoveredDevice]) -> list[ScanWarning]:
    """Avisos para el usuario: si no se pudo escuchar SADP (siempre) y, si no ha respondido nada por SADP, cada
    tarjeta de la búsqueda que Windows tiene como «Pública», una vez aunque tenga varias IP (primero las que no
    tienen Internet, que suelen ser el switch de las cámaras, y al final las virtuales)."""
    out: list[ScanWarning] = []
    st = report.sadp_listen
    if st is not None and not st.ok:
        out.append(_sadp_warning(st))
    if not any("sadp" in d.sources for d in found):
        # `discover()` anota una NicInfo por IP; el perfil (y el nombre) es de la tarjeta
        adapters: dict[str, list[NicInfo]] = {}
        for n in report.nics:
            if n.category == "public":
                adapters.setdefault(n.name, []).append(n)
        public = sorted(adapters.values(), key=lambda nics: (_is_virtual(nics[0].name), nics[0].internet is not False))
        out.extend(_public_warning(nics) for nics in public[:MAX_PUBLIC_WARNINGS])
    return out


__all__ = ["ListenStatus", "NicInfo", "ScanReport", "ScanWarning", "collecting", "current", "explain",
           "note_sadp_listen"]
