"""Mock ASGI de la API ISAPI de Hikvision (NVR, cámara IP o DVR híbrido) con autenticación Digest.

Endpoints imitados (respuestas XML con el espacio de nombres real ver20):
  GET /ISAPI/System/deviceInfo
  GET /ISAPI/ContentMgmt/InputProxy/channels[/status]      (NVR y DVR: canales IP)
  GET /ISAPI/System/Video/inputs/channels                   (DVR: entradas analógicas)
  GET /ISAPI/Streaming/channels                             (101, 102, 201, 202… y 3301… en DVR)
  GET/PUT /ISAPI/Streaming/channels/{id}                    (el PUT registra el cambio: «Corregir códec»)
  GET /ISAPI/Streaming/channels/{id}/picture                (JPEG sintético)
  GET /ISAPI/System/time[/ntpServers]                       (hora con desfase configurable)
  GET /ISAPI/System/Network/{telnetd,ssh,UPnP,EZVIZ}, /ISAPI/Security/adminAccesses
Cualquier otra ruta → 404 con <ResponseStatus> como el equipo real.

Bloqueo: tras `lock_after` peticiones con credenciales malas, responde 401 con un `<userCheck>` con
`lockStatus=lock` y `unlockTime` (formato de firmware 5.x; no verificado con hardware).

Uso con httpx sin red:  httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app),
                                          base_url="http://nvr.local", auth=httpx.DigestAuth(u, p))
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .http_auth import AuthMode, HttpAuthChecker

NS = "http://www.hikvision.com/ver20/XMLSchema"
SNAPSHOT = (Path(__file__).parent / "assets" / "snapshot.jpg").read_bytes()
XML = "application/xml; charset=UTF-8"


@dataclass
class HikChannel:
    name: str
    ip: str = "192.168.254.2"
    online: bool = True
    main_codec: str = "H.264"
    sub_codec: str = "H.264"
    main_size: tuple[int, int] = (2560, 1440)
    sub_size: tuple[int, int] = (640, 360)
    main_kbps: int = 4096
    sub_kbps: int = 512
    gov: int = 50
    fps: int = 25
    smart_codec: bool = False


def nvr_channels(n: int, offline: tuple[int, ...] = (), h265: tuple[int, ...] = ()) -> list[HikChannel]:
    """`n` canales «Cámara N» (los de `offline` sin vídeo, los de `h265` con el principal en H.265)."""
    return [HikChannel(f"Cámara {i}", f"192.168.254.{i + 1}", online=i not in offline,
                       main_codec="H.265" if i in h265 else "H.264") for i in range(1, n + 1)]


def _status(code: int, sub: str, text: str, url: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<ResponseStatus version="2.0" xmlns="{NS}">\n'
            f"<requestURL>{escape(url)}</requestURL>\n<statusCode>{code}</statusCode>\n"
            f"<statusString>{text}</statusString>\n<subStatusCode>{sub}</subStatusCode>\n</ResponseStatus>\n")


@dataclass
class HikvisionMock:
    kind: Literal["nvr", "camera", "dvr"] = "nvr"
    username: str = "admin"
    password: str = "Hik#Pass:1@"
    model: str = "DS-7608NI-K2/8P"
    serial: str = "DS-7608NI-K2/8P0820210101CCRRF12345678WCVU"
    firmware: str = "V4.30.085"
    firmware_released: str = "build 210812"
    mac: str = "c4:2f:90:f1:e6:a1"
    device_name: str = "Network Video Recorder"
    channels: list[HikChannel] = field(default_factory=lambda: [
        HikChannel("Entrada", "192.168.254.2"),
        HikChannel("Cajas", "192.168.254.3"),
        HikChannel("Almacén", "192.168.254.4", online=False),
        HikChannel("Pasillo 1", "192.168.254.5", main_codec="H.265"),
    ])
    analog: list[HikChannel] = field(default_factory=list)     # solo DVR: entradas analógicas (ids 1..N)
    ip_start: int = 0                                           # DVR: id del primer canal IP (0 = tras analógicos)
    auth_mode: AuthMode = "digest"
    user_check: bool = True                                     # 401 con <userCheck> (firmware 5.x)
    lock_after: int | None = 5
    lock_minutes: int = 30
    clock_offset_s: float = 0.0
    time_mode: str = "NTP"
    tz_hours: float | None = None   # zona del equipo; None = la del PC (misma tienda)
    ntp_server: str = "pool.ntp.org"
    security: dict[str, bool] = field(default_factory=lambda: {
        "telnetd": False, "ssh": False, "UPnP": True, "EZVIZ": True, "https": False, "sdk": True})
    requests: list[str] = field(default_factory=list)
    puts: list[tuple[str, str]] = field(default_factory=list)
    locked: bool = False

    def __post_init__(self) -> None:
        if self.kind == "camera":
            self.channels = self.channels[:1]
            if self.model.startswith("DS-76"):
                self.model, self.device_name = "DS-2CD2143G2-I", "IP CAMERA"
                self.serial = "DS-2CD2143G2-I20210315AAWRG12345678"
        if self.kind == "dvr":
            if self.model.startswith("DS-76"):
                self.model, self.device_name = "DS-7204HQHI-K1", "Embedded Net DVR"
                self.serial = "DS-7204HQHI-K10420210101CCRRF87654321WCVU"
            if not self.analog:
                self.analog = [HikChannel(f"Analógica {i}", ip="", sub_size=(704, 576)) for i in range(1, 5)]
            if not self.ip_start:
                self.ip_start = 33
        self.auth = HttpAuthChecker(self.username, self.password, realm=f"{self.model}", mode=self.auth_mode)
        inner = Starlette(routes=[
            Route("/ISAPI/System/deviceInfo", self.device_info),
            Route("/ISAPI/ContentMgmt/InputProxy/channels", self.proxy_channels),
            Route("/ISAPI/ContentMgmt/InputProxy/channels/status", self.proxy_status),
            Route("/ISAPI/System/Video/inputs/channels", self.video_inputs),
            Route("/ISAPI/Streaming/channels", self.streaming_list),
            Route("/ISAPI/Streaming/channels/{sid:int}", self.streaming_one, methods=["GET", "PUT"]),
            Route("/ISAPI/Streaming/channels/{sid:int}/picture", self.picture),
            Route("/ISAPI/System/time", self.time),
            Route("/ISAPI/System/time/ntpServers", self.ntp_servers),
            Route("/ISAPI/System/Network/{what}", self.network_flag),
            Route("/ISAPI/Security/adminAccesses", self.admin_accesses),
        ], exception_handlers={404: self._not_found})
        self.app = _AuthMiddleware(inner, self)

    # ------------------------------------------------------------------ canales
    def ip_id(self, index: int) -> int:
        """Id del canal IP número `index` (0..): en DVR empieza en `ip_start`."""
        return (self.ip_start + index) if self.kind == "dvr" else index + 1

    def by_id(self, cid: int) -> HikChannel | None:
        if self.kind == "dvr" and 1 <= cid <= len(self.analog):
            return self.analog[cid - 1]
        for i, c in enumerate(self.channels):
            if self.ip_id(i) == cid:
                return c
        return None

    def all_ids(self) -> list[int]:
        ids = list(range(1, len(self.analog) + 1)) if self.kind == "dvr" else []
        return ids + [self.ip_id(i) for i in range(len(self.channels))]

    async def _not_found(self, request: Request, exc: Exception) -> Response:
        return Response(_status(4, "notSupport", "Invalid Operation", request.url.path), 404, media_type=XML)

    # ------------------------------------------------------------------ respuestas
    async def device_info(self, request: Request) -> Response:
        dtype = {"nvr": "NVR", "camera": "IPCamera", "dvr": "DVR"}[self.kind]
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<DeviceInfo version="2.0" xmlns="{NS}">\n'
                f"<deviceName>{escape(self.device_name)}</deviceName>\n"
                "<deviceID>48443030-3637-3534-3838-c42f90f1e6a1</deviceID>\n"
                f"<model>{escape(self.model)}</model>\n<serialNumber>{escape(self.serial)}</serialNumber>\n"
                f"<macAddress>{self.mac}</macAddress>\n<firmwareVersion>{escape(self.firmware)}</firmwareVersion>\n"
                f"<firmwareReleasedDate>{escape(self.firmware_released)}</firmwareReleasedDate>\n"
                "<encoderVersion>V5.0</encoderVersion>\n<encoderReleasedDate>build 210720</encoderReleasedDate>\n"
                f"<deviceType>{dtype}</deviceType>\n<telecontrolID>255</telecontrolID>\n</DeviceInfo>\n")
        return Response(body, media_type=XML)

    async def proxy_channels(self, request: Request) -> Response:
        if self.kind == "camera":
            return await self._not_found(request, Exception())
        items = "".join(
            f'<InputProxyChannel version="2.0" xmlns="{NS}">\n<id>{self.ip_id(i)}</id>\n<name>{escape(c.name)}</name>\n'
            "<sourceInputPortDescriptor>\n<proxyProtocol>HIKVISION</proxyProtocol>\n"
            f"<addressingFormatType>ipaddress</addressingFormatType>\n<ipAddress>{c.ip}</ipAddress>\n"
            "<managePortNo>8000</managePortNo>\n<srcInputPort>1</srcInputPort>\n<userName>admin</userName>\n"
            "<streamType>auto</streamType>\n<deviceID></deviceID>\n</sourceInputPortDescriptor>\n"
            "<enableAnr>false</enableAnr>\n<enableTiming>true</enableTiming>\n</InputProxyChannel>\n"
            for i, c in enumerate(self.channels))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<InputProxyChannelList version="2.0" xmlns="{NS}" '
                f'size="{len(self.channels)}">\n{items}</InputProxyChannelList>\n')
        return Response(body, media_type=XML)

    async def proxy_status(self, request: Request) -> Response:
        if self.kind == "camera":
            return await self._not_found(request, Exception())
        items = "".join(
            f'<InputProxyChannelStatus version="2.0" xmlns="{NS}">\n<id>{self.ip_id(i)}</id>\n'
            "<sourceInputPortDescriptor>\n<proxyProtocol>HIKVISION</proxyProtocol>\n"
            f"<addressingFormatType>ipaddress</addressingFormatType>\n<ipAddress>{c.ip}</ipAddress>\n"
            "<managePortNo>8000</managePortNo>\n<srcInputPort>1</srcInputPort>\n</sourceInputPortDescriptor>\n"
            f"<online>{'true' if c.online else 'false'}</online>\n"
            f"<chanDetectResult>{'connect' if c.online else 'offline'}</chanDetectResult>\n"
            "</InputProxyChannelStatus>\n"
            for i, c in enumerate(self.channels))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<InputProxyChannelStatusList version="2.0" xmlns="{NS}">\n'
                f"{items}</InputProxyChannelStatusList>\n")
        return Response(body, media_type=XML)

    async def video_inputs(self, request: Request) -> Response:
        if self.kind != "dvr":
            return await self._not_found(request, Exception())
        items = "".join(
            f'<VideoInputChannel version="2.0" xmlns="{NS}">\n<id>{i}</id>\n<inputPort>{i}</inputPort>\n'
            f"<videoInputEnabled>true</videoInputEnabled>\n<name>{escape(c.name)}</name>\n"
            f"<videoFormat>PAL</videoFormat>\n<resDesc>{'1920*1080P25' if c.online else 'NO VIDEO'}</resDesc>\n"
            "</VideoInputChannel>\n" for i, c in enumerate(self.analog, start=1))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<VideoInputChannelList version="2.0" xmlns="{NS}">\n'
                f"{items}</VideoInputChannelList>\n")
        return Response(body, media_type=XML)

    def _streaming_channel(self, sid: int) -> str | None:
        cid, stream = divmod(sid, 100)
        c = self.by_id(cid)
        if c is None or stream not in (1, 2):
            return None
        codec = c.main_codec if stream == 1 else c.sub_codec
        w, h = c.main_size if stream == 1 else c.sub_size
        smart = "true" if c.smart_codec and stream == 1 else "false"
        return (f'<StreamingChannel version="2.0" xmlns="{NS}">\n<id>{sid}</id>\n'
                f"<channelName>{escape(c.name)}</channelName>\n<enabled>true</enabled>\n"
                "<Transport>\n<ControlProtocolList>\n<ControlProtocol>\n<streamingTransport>RTSP</streamingTransport>\n"
                "</ControlProtocol>\n</ControlProtocolList>\n</Transport>\n"
                f"<Video>\n<enabled>true</enabled>\n<videoInputChannelID>{cid}</videoInputChannelID>\n"
                f"<videoCodecType>{codec}</videoCodecType>\n<videoScanType>progressive</videoScanType>\n"
                f"<videoResolutionWidth>{w}</videoResolutionWidth>\n<videoResolutionHeight>{h}</videoResolutionHeight>\n"
                "<videoQualityControlType>VBR</videoQualityControlType>\n<fixedQuality>60</fixedQuality>\n"
                f"<vbrUpperCap>{c.main_kbps if stream == 1 else c.sub_kbps}</vbrUpperCap>\n"
                f"<maxFrameRate>{c.fps * 100}</maxFrameRate>\n"
                f"<GovLength>{c.gov}</GovLength>\n<SmartCodec>\n<enabled>{smart}</enabled>\n</SmartCodec>\n"
                "</Video>\n</StreamingChannel>\n")

    async def streaming_list(self, request: Request) -> Response:
        items = "".join(self._streaming_channel(cid * 100 + s) or "" for cid in self.all_ids() for s in (1, 2))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<StreamingChannelList version="2.0" xmlns="{NS}">\n'
                f"{items}</StreamingChannelList>\n")
        return Response(body, media_type=XML)

    async def streaming_one(self, request: Request) -> Response:
        sid = int(request.path_params["sid"])
        item = self._streaming_channel(sid)
        if item is None:
            return await self._not_found(request, Exception())
        if request.method == "PUT":
            body = (await request.body()).decode("utf-8", errors="replace")
            self.puts.append((request.url.path, body))
            m = re.search(r"<videoCodecType>([^<]*)</videoCodecType>", body)
            if not m:
                return Response(_status(6, "badXmlContent", "Invalid XML Content", request.url.path), 400,
                                media_type=XML)
            c = self.by_id(sid // 100)
            assert c is not None
            if sid % 100 == 1:
                c.main_codec = m.group(1)
            else:
                c.sub_codec = m.group(1)
            sm = re.search(r"<SmartCodec>\s*<enabled>(\w+)</enabled>", body)
            if sm and sid % 100 == 1:
                c.smart_codec = sm.group(1) == "true"
            return Response(_status(1, "ok", "OK", request.url.path), media_type=XML)
        return Response('<?xml version="1.0" encoding="UTF-8"?>\n' + item, media_type=XML)

    async def picture(self, request: Request) -> Response:
        sid = int(request.path_params["sid"])
        c = self.by_id(sid // 100)
        if c is None or sid % 100 not in (1, 2):
            return await self._not_found(request, Exception())
        if not c.online:
            return Response(_status(3, "deviceError", "Device Error", request.url.path), 500, media_type=XML)
        return Response(SNAPSHOT, media_type="image/jpeg")

    async def time(self, request: Request) -> Response:
        now = datetime.now(timezone.utc) + timedelta(seconds=self.clock_offset_s)
        # zona del equipo: la del PC (misma tienda) salvo que la prueba fije otra con `tz_hours`
        local = now.astimezone() if self.tz_hours is None else now.astimezone(timezone(timedelta(hours=self.tz_hours)))
        off = local.utcoffset() or timedelta(0)
        minutes = int(off.total_seconds() // 60)
        posix = f"CST{'-' if minutes >= 0 else '+'}{abs(minutes) // 60}:{abs(minutes) % 60:02d}:00"  # POSIX: signo al revés
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="{NS}">\n'
                f"<timeMode>{self.time_mode}</timeMode>\n<localTime>{local.isoformat(timespec='seconds')}</localTime>\n"
                f"<timeZone>{posix}</timeZone>\n</Time>\n")
        return Response(body, media_type=XML)

    async def ntp_servers(self, request: Request) -> Response:
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<NTPServerList version="2.0" xmlns="{NS}">\n<NTPServer>\n'
                "<id>1</id>\n<addressingFormatType>hostname</addressingFormatType>\n"
                f"<hostName>{self.ntp_server}</hostName>\n<portNo>123</portNo>\n"
                "<synchronizeInterval>60</synchronizeInterval>\n</NTPServer>\n</NTPServerList>\n")
        return Response(body, media_type=XML)

    async def network_flag(self, request: Request) -> Response:
        what = request.path_params["what"]
        tags = {"telnetd": "Telnetd", "ssh": "SSH", "UPnP": "UPnP", "EZVIZ": "EZVIZ"}
        if what not in tags:
            return await self._not_found(request, Exception())
        tag = tags[what]
        value = "true" if self.security.get(what) else "false"
        return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<{tag} version="2.0" xmlns="{NS}">\n'
                        f"<enabled>{value}</enabled>\n</{tag}>\n", media_type=XML)

    async def admin_accesses(self, request: Request) -> Response:
        def proto(name: str, port: int, enabled: bool) -> str:
            return (f"<AdminAccessProtocol>\n<id>{name}</id>\n<enabled>{'true' if enabled else 'false'}</enabled>\n"
                    f"<protocol>{name}</protocol>\n<portNo>{port}</portNo>\n</AdminAccessProtocol>\n")
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<AdminAccessProtocolList version="2.0" xmlns="{NS}">\n'
                + proto("HTTP", 80, True) + proto("HTTPS", 443, self.security.get("https", False))
                + proto("DEV_MANAGE", 8000, self.security.get("sdk", True)) + proto("RTSP", 554, True)
                + "</AdminAccessProtocolList>\n")
        return Response(body, media_type=XML)

    # ------------------------------------------------------------------ autenticación y bloqueo
    def auth_failed_body(self, path: str) -> str:
        if not self.user_check:
            return _status(4, "badAuthorization", "Invalid Operation", path)
        remaining = max(0, (self.lock_after or 99) - self.auth.rejected_credentials)
        if self.locked:
            return (f'<?xml version="1.0" encoding="UTF-8"?>\n<userCheck version="2.0" xmlns="{NS}">\n'
                    "<statusValue>401</statusValue>\n<statusString>Unauthorized</statusString>\n"
                    f"<lockStatus>lock</lockStatus>\n<unlockTime>{self.lock_minutes * 60}</unlockTime>\n"
                    "<retryLoginTime>0</retryLoginTime>\n</userCheck>\n")
        return (f'<?xml version="1.0" encoding="UTF-8"?>\n<userCheck version="2.0" xmlns="{NS}">\n'
                "<statusValue>401</statusValue>\n<statusString>Unauthorized</statusString>\n"
                f"<lockStatus>unlock</lockStatus>\n<unlockTime>0</unlockTime>\n<retryLoginTime>{remaining}</retryLoginTime>\n"
                "</userCheck>\n")


class _AuthMiddleware:
    def __init__(self, app: Any, mock: HikvisionMock) -> None:
        self.inner, self.mock = app, mock

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        request = Request(scope)
        self.mock.requests.append(f"{request.method} {request.url.path}")
        has_auth = bool(request.headers.get("authorization"))
        ok = self.mock.auth.check(request)
        if self.mock.locked and has_auth:
            ok = False
        if not ok:
            if has_auth and self.mock.lock_after and self.mock.auth.rejected_credentials >= self.mock.lock_after:
                self.mock.locked = True
            body = self.mock.auth_failed_body(request.url.path) if has_auth else \
                _status(4, "badAuthorization", "Invalid Operation", request.url.path)
            resp = self.mock.auth.challenge(body, XML)
            await resp(scope, receive, send)
            return
        await self.inner(scope, receive, send)
