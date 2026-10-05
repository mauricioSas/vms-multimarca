"""ONVIF genérico con un cliente SOAP propio sobre httpx (sin WSDL): información, perfiles, URIs y hora.

Por qué propio: necesitamos **Media2** (Profile T, `tr2:GetProfiles`/`tr2:GetStreamUri`), que describe
bien H.265 y que la biblioteca anterior no traía, poder reproducir respuestas grabadas con un transporte
httpx (fixtures) y controlar exactamente cuántas peticiones llevan credenciales.

- **Media2 primero** (`GetServices` dice si el equipo lo tiene); si no, Media1 (PLAN-V2 §3.2 punto 9).
- **Desfase de reloj:** `GetSystemDateAndTime` (sin autenticar) da la hora del equipo; el `Created` del
  `UsernameToken` se ajusta a esa hora, así un equipo con el reloj ±2 h no rechaza la contraseña buena.
- **Un solo intento con credenciales:** la primera operación autenticada sale sin credenciales para saber si
  el equipo usa Digest HTTP o UsernameToken (ver `_soap`); después, si el equipo responde `NotAuthorized` o
  401, el cliente queda fundido y no vuelve a mandar la contraseña (ni por la otra vía).
- Canales: cada «video source» es un canal (cámara = 1; NVR ONVIF = N). En cada canal, el perfil de más
  resolución es el principal y el siguiente el subflujo. Las rutas RTSP (parte tras host:puerto, con su
  query) van en `ChannelInfo.main_path/sub_path`; las URI nunca llevan credenciales.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx

from vms.core import rtsp
from vms.core.errors import (DeviceAuthFailed, DeviceError, DeviceProtocolError, DeviceUnreachable,
                             DeviceUnsupported)
from vms.core.interfaces import ChannelInfo, DeviceInfo, DeviceSecuritySettings, DeviceTime
from vms.core.models import DeviceBase, Vendor

from ._http import USER_AGENT, VendorAuth, VendorHttp, device_label
from .errors import BasicNotAllowed
from .clock import Stopwatch
from .codec import normalize_codec

log = logging.getLogger("vms.vendors.onvif")

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_PW_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
_B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"

_AUTH_MARKERS = ("notauthorized", "not authorized", "unauthorized", "authority", "authentication")
# «401» solo como código suelto: no dentro de un puerto o una IP (p. ej. «10.0.0.2:4010»).
_AUTH_401_RE = re.compile(r"(?<![\d.:])401(?!\d)")

ONVIF_OFF_HINT = ("Comprueba que ONVIF esté activado en el equipo (en Hikvision viene desactivado desde el "
                  "firmware 5.5 y usa usuarios propios) y el puerto ONVIF")


def rtsp_path_of(uri: str) -> str:
    """«rtsp://u:p@10.0.0.2:554/Streaming/Channels/101?x=1» → «/Streaming/Channels/101?x=1»."""
    parts = urlsplit(uri)
    path = parts.path or "/"
    return path + (f"?{parts.query}" if parts.query else "")


def _translate(exc: BaseException, label: str) -> DeviceError:
    """Traduce cualquier excepción de una llamada ONVIF a un error de dominio (lo usa también el motor)."""
    if isinstance(exc, DeviceError):
        return exc
    text = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in text:
        return DeviceUnreachable(f"El equipo {label} no responde por ONVIF (tiempo agotado)")
    if any(m in text for m in _AUTH_MARKERS) or _AUTH_401_RE.search(text):
        return DeviceAuthFailed(f"Usuario o contraseña ONVIF incorrectos en {label}. Comprueba también que "
                                "el usuario ONVIF esté creado en el equipo y que su hora sea correcta.")
    if isinstance(exc, (OSError, ConnectionError)) or "cannot connect" in text or "connect" in text:
        return DeviceUnreachable(f"No se puede conectar por ONVIF con {label}: revisa la IP y el puerto ONVIF")
    return DeviceProtocolError(f"Error ONVIF en {label}: {type(exc).__name__}")


def _strip(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    v = el.findtext(path)
    return v.strip() if v else ""


def password_digest(nonce: bytes, created: str, password: str) -> str:
    return base64.b64encode(hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()).decode()


class _Profile:
    def __init__(self, el: ET.Element) -> None:
        self.token = el.get("token", "")
        self.name = _t(el, "Name")
        enc = el.find("VideoEncoderConfiguration")
        if enc is None:
            enc = el.find("Configurations/VideoEncoder")
        self.has_video = enc is not None
        src = el.find("VideoSourceConfiguration")
        if src is None:
            src = el.find("Configurations/VideoSource")
        self.source = _t(src, "SourceToken") or "default"
        self.codec = normalize_codec(_t(enc, "Encoding")) if enc is not None else None
        w, h = _t(enc, "Resolution/Width"), _t(enc, "Resolution/Height")
        self.width, self.height = int(w) if w.isdigit() else 0, int(h) if h.isdigit() else 0
        br = _t(enc, "RateControl/BitrateLimit")
        self.kbps = int(br) if br.isdigit() else None
        fps = _t(enc, "RateControl/FrameRateLimit")
        # Media2: GovLength es atributo de VideoEncoder; Media1: elemento dentro de H264
        gov = ((enc.get("GovLength") or "") if enc is not None else "") or _t(enc, "GovLength") \
            or _t(enc, "H264/GovLength") or _t(enc, "H265/GovLength")
        try:
            self.gop_s: float | None = round(int(gov) / float(fps), 2) if gov and fps and float(fps) > 0 else None
        except ValueError:
            self.gop_s = None

    @property
    def resolution(self) -> str | None:
        return f"{self.width}x{self.height}" if self.width and self.height else None


class OnvifClient:
    vendor: Vendor = "onvif"

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None, port: int | None = None) -> None:
        self.device = device
        self._password = password
        self.timeout = timeout
        self.port = port or device.onvif_port or device.http_port
        self.label = device_label(device)
        self._transport = transport
        self._auth = VendorAuth(device.username, password, allow_basic=device.allow_basic) if device.username else None
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)), verify=False,
                                        follow_redirects=False, transport=transport, auth=self._auth,
                                        headers={"User-Agent": USER_AGENT})
        scheme = "https" if device.https else "http"
        self.base = f"{scheme}://{rtsp.format_host(device.host)}:{int(self.port)}"
        self.device_xaddr = f"{self.base}/onvif/device_service"
        self.media_xaddr: str | None = None
        self.media2_xaddr: str | None = None
        self.clock_offset = timedelta(0)
        self.ws_credentialed = 0           # peticiones con UsernameToken (para las pruebas)
        self._auth_mode: Literal["unknown", "ws", "http"] = "unknown"
        self._failure: DeviceError | None = None
        self._services_done = False
        self._profiles: list[_Profile] | None = None
        self._info: DeviceInfo | None = None
        self.device_utc: datetime | None = None
        self.time_mode: Literal["ntp", "manual", "unknown"] = "unknown"
        self._measured = datetime.now(timezone.utc)
        self._rtt = 0.0
        if device.vendor and device.vendor != "onvif":
            self.vendor = device.vendor     # perfiles de marca que usan ONVIF para leer el equipo

    @property
    def credentialed_requests(self) -> int:
        return self.ws_credentialed + (self._auth.credentialed if self._auth else 0)

    @property
    def uses_media2(self) -> bool:
        return self.media2_xaddr is not None

    # ------------------------------------------------------------------ SOAP
    def _fix_xaddr(self, url: str) -> str:
        """Equipos tras NAT o con varias IP anuncian XAddrs internas: se usa la dirección configurada."""
        parts = urlsplit(url)
        if not parts.scheme or (parts.hostname or "").lower() == self.device.host.strip("[]").lower():
            return url if parts.scheme else self.base + url
        base = urlsplit(self.base)
        return urlunsplit((base.scheme, base.netloc, parts.path or "/", parts.query, ""))

    def _security_header(self) -> str:
        if not self.device.username:
            return ""
        nonce = os.urandom(16)
        created = (datetime.now(timezone.utc) + self.clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.ws_credentialed += 1
        return (f'<s:Header><wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}" s:mustUnderstand="1">'
                f"<wsse:UsernameToken><wsse:Username>{escape(self.device.username)}</wsse:Username>"
                f'<wsse:Password Type="{_PW_DIGEST}">{password_digest(nonce, created, self._password)}</wsse:Password>'
                f'<wsse:Nonce EncodingType="{_B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
                f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>")

    async def _post(self, url: str, ns: str, op: str, body: str, *, header: str, http_auth: bool) -> httpx.Response:
        envelope = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                    f'xmlns:tt="{NS_SCHEMA}" xmlns:m="{ns}">{header}<s:Body><m:{op}>{body}</m:{op}></s:Body></s:Envelope>')
        try:
            # httpx.Auth() (no hace nada) desactiva VendorAuth en esta petición: solo firma con Digest/Basic HTTP el
            # modo «http»
            no_auth: httpx.Auth = httpx.Auth()
            return await self.client.post(url, content=envelope.encode("utf-8"),
                                          auth=(self._auth or no_auth) if http_auth else no_auth,
                                          headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{ns}/{op}"'})
        except DeviceAuthFailed:
            raise
        except httpx.TimeoutException as exc:
            raise DeviceUnreachable(f"El equipo {self.label} no responde por ONVIF (tiempo agotado)") from exc
        except httpx.ConnectError as exc:
            raise DeviceUnreachable(f"No se puede conectar por ONVIF con {self.label}. {ONVIF_OFF_HINT}") from exc
        except httpx.HTTPError as exc:
            raise DeviceUnreachable(f"Error de red ONVIF con {self.label}: {type(exc).__name__}") from exc

    def _bad_password(self) -> DeviceAuthFailed:
        return DeviceAuthFailed(f"Usuario o contraseña ONVIF incorrectos en {self.label}. En algunas marcas el "
                                "usuario ONVIF es distinto del de la web (Hikvision): créalo en el equipo.")

    def _fuse(self, err: DeviceError) -> DeviceError:
        self._failure = err
        return err

    async def _soap(self, url: str, ns: str, op: str, body: str = "", *, auth: bool = True) -> ET.Element:
        """Una operación SOAP con **una sola** petición con credenciales como mucho.

        Modos de autenticación (`_auth_mode`), aprendidos sin gastar la contraseña:
        - «unknown»: la primera operación autenticada sale **sin** credenciales. Un 401 con reto Digest dice que
          el equipo autentica por HTTP («http»); un Fault NotAuthorized (o cualquier otro error SOAP) dice que
          usa el UsernameToken de WS-Security («ws»). Después se repite una vez con las credenciales del modo.
        - «ws»: UsernameToken y nada más. Si el equipo responde 401 (aunque ofrezca Digest), no se reintenta
          con Digest: sería mandar la misma contraseña dos veces (criterio 4 de B5).
        - «http»: Digest (o Basic con `allow_basic`) firmado a la primera, sin UsernameToken.
        Las operaciones sin autenticar (`auth=False`, p. ej. la hora) nunca llevan credenciales; si responden 401,
        solo sirven para aprender el reto.
        """
        if self._failure is not None:
            raise self._failure
        if self._auth is not None and self._auth.rejected:
            raise self._fuse(self._bad_password())
        if not auth or self._auth is None:
            return await self._exchange(url, ns, op, body, kind="pre")
        if self._auth_mode == "unknown":
            return await self._exchange(url, ns, op, body, kind="learn")
        return await self._exchange(url, ns, op, body, kind=self._auth_mode)

    async def _exchange(self, url: str, ns: str, op: str, body: str, *,
                        kind: Literal["pre", "learn", "ws", "http"]) -> ET.Element:
        header = self._security_header() if kind == "ws" else ""
        resp = await self._post(url, ns, op, body, header=header, http_auth=kind == "http")
        text = resp.text
        root: ET.Element | None = None
        try:
            root = _strip(ET.fromstring(resp.content)) if resp.content else None
        except ET.ParseError:
            root = None
        fault = root.find(".//Fault") if root is not None else None
        not_authorized = fault is not None and "notauthorized" in ET.tostring(fault, encoding="unicode").lower()
        if kind == "learn":
            if resp.status_code == 401:
                assert self._auth is not None
                if self._auth.learn(resp):
                    self._auth_mode = "http"
                elif self._auth.basic_only:
                    raise self._fuse(BasicNotAllowed(self.label))
                else:
                    self._auth_mode = "ws"
                return await self._exchange(url, ns, op, body, kind=self._auth_mode)
            if resp.status_code in (404, 405) and root is None:
                raise DeviceUnsupported(f"{self.label} no responde a ONVIF en {urlsplit(url).path}. {ONVIF_OFF_HINT}")
            self._auth_mode = "ws"
            if not_authorized or fault is not None or resp.status_code >= 400:
                return await self._exchange(url, ns, op, body, kind="ws")
            # respondió sin credenciales: se usa la respuesta (el equipo admite ONVIF anónimo para esta operación)
        if resp.status_code == 401 or not_authorized:
            if kind == "pre":
                # sin credenciales no se ha gastado ningún intento: solo se aprende el reto, si lo hay
                if resp.status_code == 401 and self._auth is not None and self._auth_mode == "unknown" \
                        and self._auth.learn(resp):
                    self._auth_mode = "http"
                raise self._bad_password()
            raise self._fuse(self._bad_password())
        if fault is not None or resp.status_code >= 400:
            reason = _t(fault, ".//Text") if fault is not None else ""
            sub = _t(fault, ".//Subcode/Value") if fault is not None else ""
            if resp.status_code in (404, 405) and root is None:
                raise DeviceUnsupported(f"{self.label} no responde a ONVIF en {urlsplit(url).path}. {ONVIF_OFF_HINT}")
            raise DeviceProtocolError(f"Error ONVIF en {self.label} ({op}: {sub or resp.status_code} {reason[:80]})".strip(),
                                      details={"status": resp.status_code, "fault": sub})
        if root is None:
            raise DeviceProtocolError(f"{self.label} respondió a {op} con algo que no es SOAP ({text[:40]!r})")
        body_el = root.find("Body")
        if body_el is None or not len(body_el):
            raise DeviceProtocolError(f"{self.label} respondió a {op} sin contenido")
        return body_el[0]

    # ------------------------------------------------------------------ hora y servicios
    async def _sync_clock(self, *, auth: bool = False) -> None:
        if self.device_utc is not None:
            return
        try:
            with Stopwatch() as sw:
                resp = await self._soap(self.device_xaddr, NS_DEVICE, "GetSystemDateAndTime", auth=auth)
        except DeviceAuthFailed:
            if auth:
                raise
            return   # algunos equipos piden autenticación hasta para la hora; sin credenciales no gasta intento
        except DeviceError as exc:
            if isinstance(exc, DeviceUnreachable):
                raise
            log.debug("GetSystemDateAndTime no disponible en %s: %s", self.label, exc)
            return
        sdt = resp.find("SystemDateAndTime")
        utc = sdt.find("UTCDateTime") if sdt is not None else None
        if utc is None:
            return
        try:
            dev = datetime(int(_t(utc, "Date/Year")), int(_t(utc, "Date/Month")), int(_t(utc, "Date/Day")),
                           int(_t(utc, "Time/Hour")), int(_t(utc, "Time/Minute")), int(_t(utc, "Time/Second")),
                           tzinfo=timezone.utc)
        except ValueError:
            return
        self.device_utc = dev
        self._measured = sw.midpoint
        self._rtt = sw.round_trip_ms
        self.clock_offset = dev - sw.midpoint
        if abs(self.clock_offset) > timedelta(seconds=5):
            log.info("%s tiene el reloj desfasado %.0f s; se ajusta la autenticación ONVIF", self.label,
                     self.clock_offset.total_seconds())
        dtype = _t(sdt, "DateTimeType").lower()
        self.time_mode = "ntp" if dtype == "ntp" else "manual" if dtype == "manual" else "unknown"

    async def _services(self) -> None:
        if self._services_done:
            return
        await self._sync_clock()
        try:
            resp = await self._soap(self.device_xaddr, NS_DEVICE, "GetServices",
                                    "<m:IncludeCapability>false</m:IncludeCapability>")
            for svc in resp.iter("Service"):
                ns, xaddr = _t(svc, "Namespace"), _t(svc, "XAddr")
                if ns == NS_MEDIA2:
                    self.media2_xaddr = self._fix_xaddr(xaddr)
                elif ns == NS_MEDIA:
                    self.media_xaddr = self._fix_xaddr(xaddr)
        except DeviceAuthFailed:
            raise
        except DeviceError as exc:
            if isinstance(exc, DeviceUnreachable):
                raise
            log.debug("GetServices no disponible en %s (%s); se usa GetCapabilities", self.label, exc)
            resp = await self._soap(self.device_xaddr, NS_DEVICE, "GetCapabilities", "<m:Category>All</m:Category>")
            media = resp.find(".//Media/XAddr")
            if media is not None and media.text:
                self.media_xaddr = self._fix_xaddr(media.text.strip())
        if self.media_xaddr is None and self.media2_xaddr is None:
            self.media_xaddr = f"{self.base}/onvif/media_service"
        self._services_done = True

    # ------------------------------------------------------------------ DeviceClient
    async def probe(self) -> DeviceInfo:
        await self._sync_clock()
        resp = await self._soap(self.device_xaddr, NS_DEVICE, "GetDeviceInformation")
        await self._services()
        manufacturer = _t(resp, "Manufacturer")
        groups = await self._grouped_profiles()
        mac = ""
        try:
            ni = await self._soap(self.device_xaddr, NS_DEVICE, "GetNetworkInterfaces")
            mac = _t(ni, ".//Info/HwAddress").lower()
        except DeviceAuthFailed:
            raise
        except DeviceError as exc:
            log.debug("GetNetworkInterfaces no disponible en %s: %s", self.label, exc)
        info = DeviceInfo(
            vendor=self.vendor, kind="nvr" if len(groups) > 1 else "camera",
            model=_t(resp, "Model"), serial=_t(resp, "SerialNumber"), firmware=_t(resp, "FirmwareVersion"),
            name=manufacturer, manufacturer=manufacturer, mac=mac, channel_count=max(len(groups), 1))
        self._info = info
        return info

    async def _get_profiles(self) -> list[_Profile]:
        if self._profiles is None:
            await self._services()
            if self.media2_xaddr:
                resp = await self._soap(self.media2_xaddr, NS_MEDIA2, "GetProfiles", "<m:Type>All</m:Type>")
            else:
                assert self.media_xaddr is not None
                resp = await self._soap(self.media_xaddr, NS_MEDIA, "GetProfiles")
            self._profiles = [_Profile(el) for el in resp if el.tag == "Profiles"]
        return self._profiles

    async def _grouped_profiles(self) -> list[list[_Profile]]:
        """Perfiles agrupados por video source, cada grupo ordenado de mayor a menor resolución."""
        groups: dict[str, list[_Profile]] = {}
        for p in await self._get_profiles():
            if p.has_video:
                groups.setdefault(p.source, []).append(p)
        return [sorted(g, key=lambda p: p.width * p.height, reverse=True) for g in groups.values()]

    async def _stream_uri(self, token: str) -> str:
        if self.media2_xaddr:
            resp = await self._soap(self.media2_xaddr, NS_MEDIA2, "GetStreamUri",
                                    f"<m:Protocol>RTSP</m:Protocol><m:ProfileToken>{escape(token)}</m:ProfileToken>")
            uri = _t(resp, "Uri")
        else:
            assert self.media_xaddr is not None
            resp = await self._soap(
                self.media_xaddr, NS_MEDIA, "GetStreamUri",
                "<m:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>"
                f"</tt:Transport></m:StreamSetup><m:ProfileToken>{escape(token)}</m:ProfileToken>")
            uri = _t(resp, "MediaUri/Uri")
        if not uri.lower().startswith("rtsp"):
            raise DeviceProtocolError(f"{self.label} devolvió una URI de vídeo no RTSP")
        return uri

    async def list_channels(self) -> list[ChannelInfo]:
        groups = await self._grouped_profiles()
        if not groups:
            raise DeviceProtocolError(f"{self.label} no tiene perfiles de vídeo ONVIF")
        out: list[ChannelInfo] = []
        for i, group in enumerate(groups, start=1):
            main_p = group[0]
            sub_p = group[1] if len(group) > 1 else None
            main_uri = await self._stream_uri(main_p.token)
            sub_uri = await self._stream_uri(sub_p.token) if sub_p is not None else None
            main_port = urlsplit(main_uri).port
            if main_port and main_port != self.device.rtsp_port:
                log.warning("%s anuncia RTSP en el puerto %s pero el equipo está configurado con %s",
                            self.label, main_port, self.device.rtsp_port)
            out.append(ChannelInfo(
                channel=i, name=(f"Canal {i}" if len(groups) > 1 or not main_p.name else main_p.name), online=True,
                has_sub=sub_p is not None, main_codec=main_p.codec, sub_codec=sub_p.codec if sub_p else None,
                main_resolution=main_p.resolution, sub_resolution=sub_p.resolution if sub_p else None,
                main_bitrate_kbps=main_p.kbps, sub_bitrate_kbps=sub_p.kbps if sub_p else None,
                gop_seconds=main_p.gop_s,
                main_path=rtsp_path_of(main_uri), sub_path=rtsp_path_of(sub_uri) if sub_uri else None))
        return out

    async def snapshot_uri(self, channel: int, stream: Literal["main", "sub"] = "main") -> str:
        """URI HTTP de la imagen del canal (GetSnapshotUri); no descarga nada."""
        groups = await self._grouped_profiles()
        if not 1 <= channel <= len(groups):
            raise DeviceProtocolError(f"El canal {channel} no existe en {self.label}")
        group = groups[channel - 1]
        profile = group[1] if stream == "sub" and len(group) > 1 else group[0]
        if self.media2_xaddr:
            resp = await self._soap(self.media2_xaddr, NS_MEDIA2, "GetSnapshotUri",
                                    f"<m:ProfileToken>{escape(profile.token)}</m:ProfileToken>")
            return _t(resp, "Uri")
        assert self.media_xaddr is not None
        resp = await self._soap(self.media_xaddr, NS_MEDIA, "GetSnapshotUri",
                                f"<m:ProfileToken>{escape(profile.token)}</m:ProfileToken>")
        return _t(resp, "MediaUri/Uri")

    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        uri = await self.snapshot_uri(channel, stream)
        if not uri.lower().startswith("http"):
            raise DeviceUnsupported(f"{self.label} no ofrece snapshot por ONVIF")
        parts = urlsplit(self._fix_xaddr(uri))
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        snap_dev = self.device.model_copy(update={"host": parts.hostname or self.device.host,
                                                  "https": parts.scheme == "https"})
        http = VendorHttp(snap_dev, self._password, timeout=self.timeout, transport=self._transport,
                          port=parts.port or (443 if parts.scheme == "https" else 80))
        try:
            resp_http = await http.get(path)
        finally:
            await http.aclose()
        if not resp_http.content.startswith(b"\xff\xd8"):
            raise DeviceProtocolError(f"El equipo {self.label} no devolvió una imagen JPEG")
        return resp_http.content

    # ------------------------------------------------------------------ TIME_READ
    async def device_time(self) -> DeviceTime:
        """`GetSystemDateAndTime` sin autenticar (CONTRATO §18.3)."""
        self.device_utc = None
        await self._sync_clock()
        if self.device_utc is None and self._auth_mode == "http" and self._failure is None:
            await self._sync_clock(auth=True)   # equipos que piden Digest HTTP hasta para la hora
        if self.device_utc is None:
            raise DeviceProtocolError(f"{self.label} no devolvió su hora por ONVIF")
        ntp = ""
        if self.time_mode == "ntp" and self.device.username and self._failure is None:
            try:
                r = await self._soap(self.device_xaddr, NS_DEVICE, "GetNTP")
                ntp = _t(r, ".//DNSname") or _t(r, ".//IPv4Address")
            except DeviceAuthFailed:
                raise
            except DeviceError as exc:
                log.debug("GetNTP no disponible en %s: %s", self.label, exc)
        return DeviceTime(device_time=self.device_utc, measured_at=self._measured,
                          round_trip_ms=round(self._rtt, 1), time_mode=self.time_mode, ntp_server=ntp, source="onvif")

    # ------------------------------------------------------------------ SECURITY_READ
    async def security_settings(self, admin_username: str, admin_password: str) -> DeviceSecuritySettings:
        """ONVIF solo permite saber si responde sin credenciales (`anonymous_onvif`)."""
        anon = OnvifClient(self.device.model_copy(update={"username": ""}), "", timeout=self.timeout,
                           transport=self._transport, port=self.port)
        out = DeviceSecuritySettings()
        try:
            await anon._soap(anon.device_xaddr, NS_DEVICE, "GetDeviceInformation", auth=False)
            out.anonymous_onvif = True
        except DeviceAuthFailed:
            out.anonymous_onvif = False
        except DeviceError as exc:
            log.debug("Comprobación de ONVIF anónimo en %s: %s", self.label, exc)
        finally:
            await anon.aclose()
        return out

    async def aclose(self) -> None:
        await self.client.aclose()


__all__ = ["OnvifClient", "rtsp_path_of", "password_digest", "NS_DEVICE", "NS_MEDIA", "NS_MEDIA2"]
