"""Cliente HTTP común para las API de los fabricantes (ISAPI, CGI, snapshots ONVIF).

- Autenticación Digest (la que exigen Hikvision y Dahua por defecto) con una sola ronda de
  desafío: si la contraseña es mala se devuelve DeviceAuthFailed y NO se reintenta, porque
  muchos equipos bloquean el usuario tras varios fallos seguidos.
- Timeouts en todas las llamadas.
- Traduce los errores de red/HTTP a los errores de dominio de vms.core.errors con mensajes
  en español. Nunca incluye la contraseña en mensajes ni registros.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from vms.core import rtsp
from vms.core.errors import DeviceAuthFailed, DeviceProtocolError, DeviceUnreachable
from vms.core.models import DeviceBase

log = logging.getLogger("vms.vendors.http")

MAX_BODY_BYTES = 8 * 1024 * 1024  # ninguna respuesta de configuración ni snapshot debería pasar de aquí


def base_url(device: DeviceBase, port: int | None = None) -> str:
    scheme = "https" if device.https else "http"
    return f"{scheme}://{rtsp.format_host(device.host)}:{int(port or device.http_port)}"


def device_label(device: DeviceBase) -> str:
    return f"{device.name} ({device.host})"


class VendorHttp:
    """Envoltorio de httpx.AsyncClient con Digest, timeout y traducción de errores."""

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None, port: int | None = None,
                 auth: str = "digest") -> None:
        self.device = device
        self.label = device_label(device)
        auth_obj: httpx.Auth | None = None
        if device.username:
            auth_obj = (httpx.DigestAuth(device.username, password) if auth == "digest"
                        else httpx.BasicAuth(device.username, password))
        self.client = httpx.AsyncClient(
            base_url=base_url(device, port), auth=auth_obj, transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            verify=False,  # los equipos usan certificados autofirmados; solo red local/VPN
            follow_redirects=False,
            headers={"User-Agent": "VMSMultimarca/1.0"},
        )

    async def get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None, *,
                  ok_404: bool = False) -> httpx.Response:
        try:
            resp = await self.client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise DeviceUnreachable(f"El equipo {self.label} no responde (tiempo agotado)") from exc
        except httpx.ConnectError as exc:
            raise DeviceUnreachable(f"No se puede conectar con {self.label}: comprueba la IP, el puerto "
                                    "HTTP y que el equipo esté encendido") from exc
        except httpx.HTTPError as exc:
            raise DeviceUnreachable(f"Error de red al hablar con {self.label}: {type(exc).__name__}") from exc
        if resp.status_code == 401:
            raise DeviceAuthFailed(f"Usuario o contraseña incorrectos en {self.label}. Ojo: tras varios "
                                   "intentos fallidos el equipo puede bloquear el usuario un rato.")
        if resp.status_code == 403:
            raise DeviceAuthFailed(f"El usuario no tiene permisos suficientes en {self.label}")
        if resp.status_code == 404 and ok_404:
            return resp
        if resp.status_code >= 400:
            raise DeviceProtocolError(
                f"{self.label} respondió con un error {resp.status_code} a {path}",
                details={"status": resp.status_code})
        if len(resp.content) > MAX_BODY_BYTES:
            raise DeviceProtocolError(f"Respuesta demasiado grande de {self.label}")
        return resp

    async def aclose(self) -> None:
        await self.client.aclose()
