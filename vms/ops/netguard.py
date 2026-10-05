"""Destinos de salida que configura un administrador (webhook de avisos): solo servidores PÚBLICOS por HTTPS.

Sin esta comprobación, la URL del webhook es un SSRF: el backend haría POST a lo que se le diga desde el PC de
la tienda (la API de MediaMTX en 127.0.0.1, el panel del router, `169.254.169.254`…) y el registro del envío
contaría si el puerto estaba abierto. Se rechazan loopback, privadas, link-local, CGNAT, multicast, reservadas y
no especificadas, también después de resolver el nombre (un DNS puede apuntar a 127.0.0.1). No se siguen
redirecciones (`notify.http_post`).

Lo que queda: entre la resolución y la conexión el DNS podría cambiar (DNS rebinding). Se acepta: el atacante
tendría que ser administrador del VMS y controlar el DNS, y la respuesta del servidor no se muestra a nadie.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeUrl(ValueError):
    """La URL no es un destino público por HTTPS. El texto va en español para la interfaz."""


def _forbidden(ip: IpAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local
            or ip.is_private or ip.is_reserved)


def check_url(url: str) -> str:
    """Comprobación sin red (al guardar los ajustes): `https://`, con host, y si el host es una IP, pública.
    Devuelve el host. Lanza `UnsafeUrl`."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        _ = parts.port   # puerto no numérico → ValueError
    except ValueError as exc:
        raise UnsafeUrl("La dirección del webhook no es válida") from exc
    if parts.scheme.lower() != "https":
        raise UnsafeUrl("La dirección del webhook tiene que empezar por https://")
    if not host:
        raise UnsafeUrl("A la dirección del webhook le falta el servidor")
    if parts.username or parts.password:
        raise UnsafeUrl("La dirección del webhook no puede llevar usuario ni contraseña: usa el secreto de firma")
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrl("El webhook tiene que ser un servidor de Internet, no uno de la red local")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if _forbidden(ip):
        raise UnsafeUrl("El webhook tiene que ser un servidor de Internet, no uno de la red local ni de este PC")
    return host


async def resolve_public(url: str) -> None:
    """`check_url` y además cada dirección a la que resuelve el nombre tiene que ser pública. Lanza `UnsafeUrl`."""
    host = check_url(url)
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrl("No se encuentra el servidor del webhook (DNS)") from exc
    addrs = {str(info[4][0]) for info in infos}
    if not addrs:
        raise UnsafeUrl("No se encuentra el servidor del webhook (DNS)")
    for addr in addrs:
        if _forbidden(ipaddress.ip_address(addr.split("%", 1)[0])):
            raise UnsafeUrl("El nombre del webhook apunta a la red local o a este PC: no se envía")


__all__ = ["UnsafeUrl", "check_url", "resolve_public"]
