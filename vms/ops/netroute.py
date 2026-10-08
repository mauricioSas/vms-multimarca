"""Por qué tarjeta de red sale este PC hacia un equipo: paso `route` de «¿Por qué no conecta?» (CONTRATO §18.11).

Lo que pasó en el PC de la tienda (octubre de 2026): «Ethernet» (192.168.1.80/24, al switch de la cámara) y
«Wi-Fi» (192.168.1.141/24, al router de casa) están en la misma red 192.168.1.0/24.
- Con el cable de «Ethernet» desconectado, Windows manda el tráfico de la cámara (192.168.1.64) por «Wi-Fi», donde
  nadie contesta. El diagnóstico solo decía «El equipo no responde en la red» y hablaba del cable de la cámara.
- Con el cable puesto, «Ethernet» (métrica automática 25 a 1 Gbit/s) gana a «Wi-Fi» (30) y todo funciona. Con un
  enlace de 100 Mbit/s (métrica 35), o si cambia la del WiFi, Windows pasaría a usar «Wi-Fi».

No manda tráfico nuevo. La IP de origen se le pregunta a Windows con `connect()` en UDP, que solo elige la ruta y no
envía ningún paquete; las tarjetas y su estado de enlace se leen con psutil (también las desconectadas con IP fija).
Si el equipo contesta se sabe por las pruebas TCP que el diagnóstico ya hace, que salen por la tarjeta que elige
Windows. `assess` es lógica pura: las pruebas le pasan una foto falsa de la red (`NetView`).

No se avisa de una puerta de enlace «sin router»: con el cable desconectado no contesta ningún vecino y no se
distingue un caso del otro. Las 169.254 de tarjetas sin enlace (Bluetooth, WiFi Direct) no cuentan.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("vms.ops.netroute")

MAX_ADAPTERS = 16
DISCARD_PORT = 9          # da igual cuál: con UDP, connect() no envía nada

Reason = Literal["direct", "router", "link_down", "shared_subnet", "other_network", "other_route", "unknown"]


@dataclass(frozen=True)
class Addr:
    ip: str
    prefix: int

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.ip}/{self.prefix}", strict=False)

    @property
    def link_local(self) -> bool:
        return ipaddress.IPv4Address(self.ip).is_link_local


@dataclass(frozen=True)
class Adapter:
    name: str                       # nombre de la tarjeta en Windows («Ethernet», «Wi-Fi»)
    up: bool                        # con enlace: cable conectado o WiFi asociado
    addrs: tuple[Addr, ...]         # IPv4, también las de una tarjeta desconectada con IP fija

    def ip_in(self, host: ipaddress.IPv4Address) -> str | None:
        """La IP de esta tarjeta en la red de `host` (None si no está en esa red)."""
        return next((a.ip for a in self.addrs if host in a.network), None)

    def net_of(self, host: ipaddress.IPv4Address) -> ipaddress.IPv4Network | None:
        return next((a.network for a in self.addrs if host in a.network), None)


@dataclass(frozen=True)
class NetView:
    """Foto de la red de este PC hacia un equipo."""
    adapters: tuple[Adapter, ...]
    source_ip: str | None           # IP con la que Windows sale hacia el equipo (None: no hay ruta)

    def via(self) -> Adapter | None:
        """La tarjeta por la que sale el tráfico del equipo."""
        if not self.source_ip:
            return None
        return next((a for a in self.adapters if any(x.ip == self.source_ip for x in a.addrs)), None)


@dataclass(frozen=True)
class RouteCheck:
    reason: Reason
    ok: bool | None                 # False: explica por qué el equipo no contesta
    detail_es: str
    action_es: str = ""
    warning: bool = False           # aviso: no es la causa, pero conviene revisarlo


def lan_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """La IP del equipo si es una IPv4 de red local (privada o 169.254). None con un nombre, IPv6, loopback o una IP
    de Internet: entonces el paso no se hace."""
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        return None
    if not isinstance(ip, ipaddress.IPv4Address) or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return None
    return ip if ip.is_private or ip.is_link_local else None


# ============================================================================== textos
def _names(adapters: list[Adapter]) -> str:
    q = [f"«{a.name}»" for a in adapters]
    return q[0] if len(q) == 1 else ", ".join(q[:-1]) + " y " + q[-1]


def _ips(adapters: list[Adapter], host: ipaddress.IPv4Address) -> str:
    return ", ".join(a.ip_in(host) or "" for a in adapters)


def _net_text(net: ipaddress.IPv4Network | None) -> str:
    if net is None:
        return ""
    if net.prefixlen == 24:
        return ".".join(str(net.network_address).split(".")[:3]) + ".x"
    return str(net)


def _last(ip: str) -> str:
    return ip.rsplit(".", 1)[-1]


def _free_net(view: NetView) -> int:
    """La primera red 192.168.N.0/24 que no usa ninguna tarjeta (para separar la red de las cámaras)."""
    used = [a.network for ad in view.adapters for a in ad.addrs if not a.link_local]
    return next((n for n in range(2, 255)
                 if not any(ipaddress.IPv4Network(f"192.168.{n}.0/24").overlaps(u) for u in used)), 2)


def _renumber(view: NetView, host: ipaddress.IPv4Address, adapter: Adapter) -> str:
    """Consejo para separar la red de las cámaras, con `adapter` como la tarjeta del switch de las cámaras."""
    n = _free_net(view)
    pc = f"192.168.{n}.{_last(adapter.ip_in(host) or '80')}"
    dev = f"192.168.{n}.{_last(str(host))}"
    return (f"dale a la red de las cámaras otra numeración: por ejemplo, en «{adapter.name}» la IP {pc} con máscara "
            f"255.255.255.0 y sin puerta de enlace, y al equipo la {dev} (desde su web o con SADP en Hikvision; "
            "después cámbiala también en «Editar»)")


def suggested_pc_ip(host: str) -> str:
    """IP libre probable para el PC en la red /24 del equipo (la .250, o la .251 si el equipo es la .250)."""
    parts = host.split(".")
    return ".".join(parts[:3] + ["251" if parts[-1] == "250" else "250"])


# ============================================================================== reglas
def assess(host: str, view: NetView, *, answered: bool) -> RouteCheck | None:
    """Qué tiene que ver la red de este PC con que el equipo conteste o no. `answered`: si contestó a las pruebas
    TCP del diagnóstico, que salen por la tarjeta que elige Windows. None si el paso no aplica."""
    ip = lan_ipv4(host)
    if ip is None:
        return None
    ll = ip.is_link_local
    # tarjetas en la red del equipo; con una 169.254, solo las que tienen enlace (Bluetooth y WiFi Direct
    # desconectados también tienen una 169.254 y no son el camino)
    on = [a for a in view.adapters if a.ip_in(ip) and (a.up or not ll)]
    up_on = [a for a in on if a.up]
    down_on = [a for a in on if not a.up]
    via = view.via()
    src = view.source_ip or ""
    if answered:
        if via is None:
            return RouteCheck("unknown", True,
                              "El equipo contesta a este PC (no se pudo saber por qué tarjeta de red).")
        if via not in on:
            return RouteCheck("router", True, f"Este PC llega al equipo a través del router, por «{via.name}» ({src}).")
        others = [a for a in up_on if a is not via]
        if others and not ll:
            return _shared_answering(view, ip, via, others)
        return RouteCheck("direct", True, f"Este PC llega al equipo por «{via.name}» ({src}), que está en su red.")
    if down_on:
        return _link_down(ip, down_on, via, src, same_net=via is not None and via in up_on)
    if not on:
        return _other_network(host, ip, view, via)
    if via is not None and via in up_on:
        others = [a for a in up_on if a is not via]
        if others and not ll:
            return _shared_not_answering(view, ip, via, others)
        return RouteCheck("direct", True, f"Este PC busca el equipo por «{via.name}» ({src}), que está conectada y en "
                                          "su red: por este lado no hay problema.")
    if via is not None:
        return RouteCheck("other_route", None, f"Windows no busca el equipo por {_names(up_on)}, que está en su red, "
                                               f"sino por «{via.name}» ({src}).",
                          "Si tienes una VPN conectada, desconéctala y vuelve a probar.", warning=True)
    return RouteCheck("unknown", None, "No se pudo saber por qué tarjeta de red sale este PC hacia el equipo.")


def _link_down(ip: ipaddress.IPv4Address, down: list[Adapter], via: Adapter | None, src: str, *,
               same_net: bool) -> RouteCheck:
    names = _names(down)
    if len(down) == 1:
        detail = (f"La tarjeta de red {names} de este PC ({_ips(down, ip)}), la que está en la red del equipo, está "
                  "desconectada: Windows no detecta el cable.")
    else:
        detail = (f"Las tarjetas de red {names} de este PC ({_ips(down, ip)}), las que están en la red del equipo, "
                  "están desconectadas: Windows no detecta el cable.")
    if via is not None and via.up:
        detail += f" Por eso busca el equipo por «{via.name}» ({src}), y por ahí no contesta."
    action = (f"Conecta el cable de red de {names} al switch de las cámaras (o al equipo) y comprueba que se enciende "
              "la luz del puerto. Si ya está conectado, prueba con otro cable o con otro puerto del switch, y "
              "comprueba que el switch está enchufado.")
    if same_net and via is not None:
        action += f" Si el equipo no va por ese cable sino por «{via.name}», revisa que esté encendido."
    return RouteCheck("link_down", False, detail, action)


def _other_network(host: str, ip: ipaddress.IPv4Address, view: NetView, via: Adapter | None) -> RouteCheck:
    ups = [a for a in view.adapters if a.up]
    pc_ips = [x.ip for a in ups for x in a.addrs if not x.link_local] or [x.ip for a in ups for x in a.addrs]
    if not pc_ips:
        return RouteCheck("other_network", False, "Este PC no está conectado a ninguna red: ninguna tarjeta tiene el "
                                                  "cable conectado ni el WiFi activo.",
                          "Conecta el cable de red de este PC al switch de las cámaras (o activa el WiFi si el equipo "
                          "va por WiFi) y vuelve a probar.")
    how = "Windows lo busca a través del router y no contesta." if via is not None else \
        "Windows no tiene por dónde llegar a él."
    detail = f"El equipo tiene la IP {host}, que no es de la red de este PC ({', '.join(pc_ips[:3])}): {how}"
    if ip.is_link_local:
        action = ("Pon al equipo una IP fija de la red de este PC (desde su web o con SADP en Hikvision) y cámbiala "
                  "también en «Editar».")
    else:
        action = (f"Pon a la tarjeta de red del cable de las cámaras una IP fija de su red, por ejemplo "
                  f"{suggested_pc_ip(host)} con máscara 255.255.255.0 y sin puerta de enlace, o cambia la IP del "
                  "equipo a la red del PC (desde su web o con SADP en Hikvision) y cámbiala también en «Editar».")
    return RouteCheck("other_network", False, detail, action)


def _shared_answering(view: NetView, ip: ipaddress.IPv4Address, via: Adapter, others: list[Adapter]) -> RouteCheck:
    one = len(others) == 1
    detail = (f"Este PC llega al equipo por «{via.name}» ({via.ip_in(ip)}), pero {_names(others)} ({_ips(others, ip)}) "
              f"también {'está' if one else 'están'} en su red ({_net_text(via.net_of(ip))}): si Windows pasa a usar "
              f"{'esa tarjeta' if one else 'una de ellas'}, puede dejar de llegar al equipo.")
    action = (f"Si {'las dos' if one else 'todas'} van al mismo router, no pasa nada. Si «{via.name}» va a un switch "
              f"solo para las cámaras, {_renumber(view, ip, via)}.")
    return RouteCheck("shared_subnet", True, detail, action, warning=True)


def _shared_not_answering(view: NetView, ip: ipaddress.IPv4Address, via: Adapter,
                          others: list[Adapter]) -> RouteCheck:
    one = len(others) == 1
    detail = (f"Windows busca el equipo por «{via.name}» ({via.ip_in(ip)}) y por ahí no contesta, pero "
              f"{_names(others)} ({_ips(others, ip)}) también {'está' if one else 'están'} en su red "
              f"({_net_text(via.net_of(ip))}).")
    # no se sabe cuál es la del router: el ejemplo no nombra tarjeta (cambiar la del WiFi de casa sería peor)
    action = (f"Si el equipo está conectado por {_names(others)} y no por «{via.name}», Windows no le está mandando "
              "el tráfico. Si acabas de conectarlo, espera un minuto y vuelve a probar. Si sigue igual, desactiva un "
              f"momento «{via.name}» y vuelve a probar; si así contesta, deja como está la tarjeta del router y pon en "
              f"la del switch de las cámaras otra numeración, por ejemplo 192.168.{_free_net(view)}.x con máscara "
              "255.255.255.0 y sin puerta de enlace, y al equipo una IP de esa red (desde su web o con SADP en "
              "Hikvision; después cámbiala también en «Editar»).")
    # no es la causa probable: puede que el equipo esté apagado (revisión del 8/10)
    return RouteCheck("shared_subnet", None, detail, action, warning=True)


# ============================================================================== lectura del sistema
def _prefix(netmask: str | None) -> int:
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen if netmask else 24
    except ValueError:
        return 24


def source_ip(host: str) -> str | None:
    """IP de este PC con la que Windows saldría hacia `host`. Con UDP, `connect()` solo elige la ruta: no envía
    ningún paquete. Solo con una IPv4 escrita (un nombre haría una consulta DNS). None si no hay ruta."""
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, DISCARD_PORT))
        ip = str(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()
    return None if ip in ("", "0.0.0.0") else ip


def read_adapters() -> tuple[Adapter, ...] | None:
    """Tarjetas con IPv4 (sin loopback), con o sin enlace. None si no se pueden leer."""
    try:
        import psutil
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as exc:  # noqa: BLE001 - sin psutil o sin permiso: el paso dice que no se pudo saber
        log.debug("No se pudieron leer las tarjetas de red: %s", exc)
        return None
    out: list[Adapter] = []
    for name, entries in addrs.items():
        v4: list[Addr] = []
        for a in entries:
            if a.family != socket.AF_INET or not a.address:
                continue
            try:
                ip = ipaddress.IPv4Address(a.address)
            except ValueError:
                continue
            if not (ip.is_loopback or ip.is_unspecified):
                v4.append(Addr(a.address, _prefix(a.netmask)))
        if v4:
            st = stats.get(name)
            out.append(Adapter(str(name), bool(st.isup) if st is not None else True, tuple(v4)))
    return tuple(out[:MAX_ADAPTERS])


def snapshot(host: str) -> NetView | None:
    adapters = read_adapters()
    if adapters is None:
        return None
    return NetView(adapters, source_ip(host))


async def probe(host: str) -> NetView | None:
    """`snapshot` fuera del bucle de eventos (psutil tarda unos milisegundos en Windows)."""
    return await asyncio.to_thread(snapshot, host)


__all__ = ["Adapter", "Addr", "NetView", "RouteCheck", "assess", "lan_ipv4", "probe", "read_adapters", "snapshot",
           "source_ip", "suggested_pc_ip"]
