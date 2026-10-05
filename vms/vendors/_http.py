"""Cliente HTTP común para las API de los fabricantes (ISAPI, CGI, ONVIF).

- Autenticación con `VendorAuth`: lee todos los retos `WWW-Authenticate` y elige el más fuerte (Digest
  SHA-256 > MD5 > Basic, este último solo con `allow_basic`). Una sola ronda de desafío: si la contraseña
  es mala se devuelve DeviceAuthFailed (o DeviceLocked si el equipo dice que el usuario está bloqueado) y
  el cliente queda **fundido**: ninguna petición posterior vuelve a mandar credenciales.
- Timeouts en todas las llamadas.
- Traduce los errores de red/HTTP a los errores de dominio de vms.core.errors con mensajes en español.
  Nunca incluye la contraseña en mensajes ni registros.
"""
from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import httpx

from vms.core import rtsp
from vms.core.errors import DeviceAuthFailed, DeviceProtocolError, DeviceUnreachable
from vms.core.models import DeviceBase

from . import auth as vauth
from .errors import BasicNotAllowed, DeviceLocked, bad_password_message, hik_user_check, text_lock_hint

log = logging.getLogger("vms.vendors.http")

MAX_BODY_BYTES = 8 * 1024 * 1024  # ninguna respuesta de configuración ni snapshot debería pasar de aquí
USER_AGENT = "VMSMultimarca/2.0"


def base_url(device: DeviceBase, port: int | None = None) -> str:
    scheme = "https" if device.https else "http"
    return f"{scheme}://{rtsp.format_host(device.host)}:{int(port or device.http_port)}"


def device_label(device: DeviceBase) -> str:
    return f"{device.name} ({device.host})"


class VendorAuth(httpx.Auth):
    """Digest RFC 7616 / Basic con un solo intento por contraseña (ver el docstring del módulo)."""

    requires_response_body = True

    def __init__(self, username: str, password: str, *, allow_basic: bool = False) -> None:
        self.username, self._password, self.allow_basic = username, password, allow_basic
        self.challenge: vauth.Challenge | None = None
        self.state: vauth.DigestState | None = None
        self.credentialed = 0              # peticiones enviadas con credenciales
        self.rejected = False              # el equipo rechazó las credenciales: no se vuelven a mandar
        self.basic_only = False

    def __repr__(self) -> str:   # nunca la contraseña en trazas
        return f"VendorAuth(username={self.username!r})"

    def _sign(self, request: httpx.Request) -> None:
        assert self.challenge is not None
        uri = request.url.raw_path.decode("ascii", errors="replace")
        request.headers["Authorization"] = vauth.authorization(
            self.challenge, request.method, uri, self.username, self._password, state=self.state)
        self.credentialed += 1

    def learn(self, response: httpx.Response) -> bool:
        """Aprende el reto de un 401 recibido **sin** credenciales; no firma ni reenvía nada.

        Devuelve True si hay un reto utilizable (las siguientes peticiones irán firmadas a la primera).
        """
        challenges = vauth.parse_challenges(response.headers.get_list("www-authenticate"))
        chosen = vauth.choose(challenges, allow_basic=self.allow_basic)
        if chosen is None:
            self.basic_only = vauth.only_basic(challenges)
            return False
        self.challenge, self.state = chosen, vauth.DigestState(chosen)
        return True

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self.rejected:
            # Fundido: VendorHttp y OnvifClient no dejan llegar aquí. Si alguien lo hace, se corta sin enviar
            # nada (un `return` antes del primer `yield` daría «async generator raised StopIteration»).
            raise DeviceAuthFailed("Usuario o contraseña incorrectos: no se vuelve a intentar con la misma "
                                   "contraseña para no bloquear el usuario del equipo")
        if self.challenge is not None:
            self._sign(request)
        response = yield request
        if response.status_code != 401:
            return
        challenges = vauth.parse_challenges(response.headers.get_list("www-authenticate"))
        signed_before = self.challenge is not None
        stale = any(c.stale for c in challenges)
        if signed_before and not stale:
            self.rejected = True       # credenciales enviadas y rechazadas: un solo intento
            return
        chosen = vauth.choose(challenges, allow_basic=self.allow_basic)
        if chosen is None:
            self.basic_only = vauth.only_basic(challenges)
            return
        self.challenge, self.state = chosen, vauth.DigestState(chosen)
        self._sign(request)
        response = yield request
        if response.status_code == 401:
            self.rejected = True


class VendorHttp:
    """Envoltorio de httpx.AsyncClient con VendorAuth, timeout y traducción de errores."""

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None, port: int | None = None,
                 allow_basic: bool | None = None) -> None:
        self.device = device
        self.label = device_label(device)
        basic = device.allow_basic if allow_basic is None else allow_basic
        self.auth: VendorAuth | None = VendorAuth(device.username, password, allow_basic=basic) \
            if device.username else None
        self._failure: DeviceAuthFailed | None = None
        self.client = httpx.AsyncClient(
            base_url=base_url(device, port), auth=self.auth, transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            verify=False,  # los equipos usan certificados autofirmados; solo red local/VPN
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    @property
    def credentialed_requests(self) -> int:
        return self.auth.credentialed if self.auth else 0

    def _auth_error(self, resp: httpx.Response) -> DeviceAuthFailed:
        body = resp.text if resp.content and len(resp.content) < 65536 else ""
        if self.auth is not None and self.auth.basic_only:
            return BasicNotAllowed(self.label)
        check = hik_user_check(body)
        if check is not None:
            locked, minutes, remaining = check
            if locked:
                return DeviceLocked(self.label, minutes)
            return DeviceAuthFailed(bad_password_message(self.label, remaining),
                                    details={"remaining_attempts": remaining})
        locked, minutes = text_lock_hint(body)
        if locked:
            return DeviceLocked(self.label, minutes)
        if resp.status_code == 403:
            return DeviceAuthFailed(f"El usuario no tiene permisos suficientes en {self.label}")
        return DeviceAuthFailed(bad_password_message(self.label))

    async def request(self, method: str, path: str, *,
                      params: dict[str, Any] | list[tuple[str, Any]] | None = None,
                      content: bytes | str | None = None, headers: dict[str, str] | None = None,
                      ok_404: bool = False) -> httpx.Response:
        if self._failure is not None:
            raise self._failure      # fundido: ya se gastó el único intento con esta contraseña
        if self.auth is not None and self.auth.rejected:
            self._failure = DeviceAuthFailed(bad_password_message(self.label))
            raise self._failure
        try:
            resp = await self.client.request(method, path, params=params, content=content, headers=headers)
        except httpx.TimeoutException as exc:
            raise DeviceUnreachable(f"El equipo {self.label} no responde (tiempo agotado)") from exc
        except httpx.ConnectError as exc:
            raise DeviceUnreachable(f"No se puede conectar con {self.label}: comprueba la IP, el puerto "
                                    "HTTP y que el equipo esté encendido") from exc
        except httpx.HTTPError as exc:
            raise DeviceUnreachable(f"Error de red al hablar con {self.label}: {type(exc).__name__}") from exc
        if resp.status_code == 401 or (resp.status_code == 403 and self.auth is not None and self.auth.challenge):
            err = self._auth_error(resp)
            if resp.status_code == 401 or isinstance(err, DeviceLocked):
                self._failure = err
            raise err
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

    async def get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None, *,
                  ok_404: bool = False) -> httpx.Response:
        return await self.request("GET", path, params=params, ok_404=ok_404)

    async def put(self, path: str, content: bytes | str, *, content_type: str = "application/xml") -> httpx.Response:
        return await self.request("PUT", path, content=content, headers={"Content-Type": content_type})

    async def aclose(self) -> None:
        await self.client.aclose()
