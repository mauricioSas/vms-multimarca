"""Mock ASGI de la API ISAPI de Hikvision (NVR o cámara IP) con autenticación Digest.

Endpoints imitados (respuestas XML con el espacio de nombres real ver20):
  GET /ISAPI/System/deviceInfo
  GET /ISAPI/ContentMgmt/InputProxy/channels            (solo NVR)
  GET /ISAPI/ContentMgmt/InputProxy/channels/status     (solo NVR)
  GET /ISAPI/Streaming/channels                         (101, 102, 201, 202...)
  GET /ISAPI/Streaming/channels/{id}
  GET /ISAPI/Streaming/channels/{id}/picture            (JPEG sintético)
Cualquier otra ruta → 404 con <ResponseStatus> como el equipo real.

Uso con httpx sin red:  httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app),
                                          base_url="http://nvr.local", auth=httpx.DigestAuth(u, p))
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .http_auth import HttpAuthChecker

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


def _status(code: int, sub: str, text: str, url: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<ResponseStatus version="2.0" xmlns="{NS}">\n'
            f"<requestURL>{escape(url)}</requestURL>\n<statusCode>{code}</statusCode>\n"
            f"<statusString>{text}</statusString>\n<subStatusCode>{sub}</subStatusCode>\n</ResponseStatus>\n")


@dataclass
class HikvisionMock:
    kind: Literal["nvr", "camera"] = "nvr"
    username: str = "admin"
    password: str = "Hik#Pass:1@"
    model: str = "DS-7608NI-K2/8P"
    serial: str = "DS-7608NI-K2/8P0820210101CCRRF12345678WCVU"
    firmware: str = "V4.30.085"
    mac: str = "c4:2f:90:f1:e6:a1"
    device_name: str = "Network Video Recorder"
    channels: list[HikChannel] = field(default_factory=lambda: [
        HikChannel("Entrada", "192.168.254.2"),
        HikChannel("Cajas", "192.168.254.3"),
        HikChannel("Almacén", "192.168.254.4", online=False),
        HikChannel("Pasillo 1", "192.168.254.5", main_codec="H.265"),
    ])
    auth_mode: Literal["digest", "basic", "none"] = "digest"
    requests: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind == "camera":
            self.channels = self.channels[:1]
            if self.model.startswith("DS-76"):
                self.model, self.device_name = "DS-2CD2143G2-I", "IP CAMERA"
                self.serial = "DS-2CD2143G2-I20210315AAWRG12345678"
        self.auth = HttpAuthChecker(self.username, self.password, realm=f"{self.model}", mode=self.auth_mode)
        inner = Starlette(routes=[
            Route("/ISAPI/System/deviceInfo", self.device_info),
            Route("/ISAPI/ContentMgmt/InputProxy/channels", self.proxy_channels),
            Route("/ISAPI/ContentMgmt/InputProxy/channels/status", self.proxy_status),
            Route("/ISAPI/Streaming/channels", self.streaming_list),
            Route("/ISAPI/Streaming/channels/{sid:int}", self.streaming_one),
            Route("/ISAPI/Streaming/channels/{sid:int}/picture", self.picture),
        ], exception_handlers={404: self._not_found})
        self.app = _AuthMiddleware(inner, self)

    async def _not_found(self, request: Request, exc: Exception) -> Response:
        return Response(_status(4, "notSupport", "Invalid Operation", request.url.path), 404, media_type=XML)

    # ------------------------------------------------------------------ respuestas
    async def device_info(self, request: Request) -> Response:
        dtype = "NVR" if self.kind == "nvr" else "IPCamera"
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<DeviceInfo version="2.0" xmlns="{NS}">\n'
                f"<deviceName>{escape(self.device_name)}</deviceName>\n"
                "<deviceID>48443030-3637-3534-3838-c42f90f1e6a1</deviceID>\n"
                f"<model>{escape(self.model)}</model>\n<serialNumber>{escape(self.serial)}</serialNumber>\n"
                f"<macAddress>{self.mac}</macAddress>\n<firmwareVersion>{self.firmware}</firmwareVersion>\n"
                "<firmwareReleasedDate>build 210812</firmwareReleasedDate>\n<encoderVersion>V5.0</encoderVersion>\n"
                "<encoderReleasedDate>build 210720</encoderReleasedDate>\n"
                f"<deviceType>{dtype}</deviceType>\n<telecontrolID>255</telecontrolID>\n</DeviceInfo>\n")
        return Response(body, media_type=XML)

    async def proxy_channels(self, request: Request) -> Response:
        if self.kind != "nvr":
            return await self._not_found(request, Exception())
        items = "".join(
            f'<InputProxyChannel version="2.0" xmlns="{NS}">\n<id>{i}</id>\n<name>{escape(c.name)}</name>\n'
            "<sourceInputPortDescriptor>\n<proxyProtocol>HIKVISION</proxyProtocol>\n"
            f"<addressingFormatType>ipaddress</addressingFormatType>\n<ipAddress>{c.ip}</ipAddress>\n"
            "<managePortNo>8000</managePortNo>\n<srcInputPort>1</srcInputPort>\n<userName>admin</userName>\n"
            "<streamType>auto</streamType>\n<deviceID></deviceID>\n</sourceInputPortDescriptor>\n"
            "<enableAnr>false</enableAnr>\n<enableTiming>true</enableTiming>\n</InputProxyChannel>\n"
            for i, c in enumerate(self.channels, start=1))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<InputProxyChannelList version="2.0" xmlns="{NS}" '
                f'size="{len(self.channels)}">\n{items}</InputProxyChannelList>\n')
        return Response(body, media_type=XML)

    async def proxy_status(self, request: Request) -> Response:
        if self.kind != "nvr":
            return await self._not_found(request, Exception())
        items = "".join(
            f'<InputProxyChannelStatus version="2.0" xmlns="{NS}">\n<id>{i}</id>\n'
            "<sourceInputPortDescriptor>\n<proxyProtocol>HIKVISION</proxyProtocol>\n"
            f"<addressingFormatType>ipaddress</addressingFormatType>\n<ipAddress>{c.ip}</ipAddress>\n"
            "<managePortNo>8000</managePortNo>\n<srcInputPort>1</srcInputPort>\n</sourceInputPortDescriptor>\n"
            f"<online>{'true' if c.online else 'false'}</online>\n"
            f"<chanDetectResult>{'connect' if c.online else 'offline'}</chanDetectResult>\n"
            "</InputProxyChannelStatus>\n"
            for i, c in enumerate(self.channels, start=1))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<InputProxyChannelStatusList version="2.0" xmlns="{NS}">\n'
                f"{items}</InputProxyChannelStatusList>\n")
        return Response(body, media_type=XML)

    def _streaming_channel(self, sid: int) -> str | None:
        ch, stream = divmod(sid, 100)
        if not 1 <= ch <= len(self.channels) or stream not in (1, 2):
            return None
        c = self.channels[ch - 1]
        codec = c.main_codec if stream == 1 else c.sub_codec
        w, h = c.main_size if stream == 1 else c.sub_size
        return (f'<StreamingChannel version="2.0" xmlns="{NS}">\n<id>{sid}</id>\n'
                f"<channelName>{escape(c.name)}</channelName>\n<enabled>true</enabled>\n"
                "<Transport>\n<ControlProtocolList>\n<ControlProtocol>\n<streamingTransport>RTSP</streamingTransport>\n"
                "</ControlProtocol>\n</ControlProtocolList>\n</Transport>\n"
                f"<Video>\n<enabled>true</enabled>\n<videoInputChannelID>{ch}</videoInputChannelID>\n"
                f"<videoCodecType>{codec}</videoCodecType>\n<videoScanType>progressive</videoScanType>\n"
                f"<videoResolutionWidth>{w}</videoResolutionWidth>\n<videoResolutionHeight>{h}</videoResolutionHeight>\n"
                "<videoQualityControlType>VBR</videoQualityControlType>\n<fixedQuality>60</fixedQuality>\n"
                f"<vbrUpperCap>{4096 if stream == 1 else 512}</vbrUpperCap>\n<maxFrameRate>2500</maxFrameRate>\n"
                "<GovLength>50</GovLength>\n</Video>\n</StreamingChannel>\n")

    async def streaming_list(self, request: Request) -> Response:
        items = "".join(self._streaming_channel(ch * 100 + s) or ""
                        for ch in range(1, len(self.channels) + 1) for s in (1, 2))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<StreamingChannelList version="2.0" xmlns="{NS}">\n'
                f"{items}</StreamingChannelList>\n")
        return Response(body, media_type=XML)

    async def streaming_one(self, request: Request) -> Response:
        item = self._streaming_channel(int(request.path_params["sid"]))
        if item is None:
            return await self._not_found(request, Exception())
        return Response('<?xml version="1.0" encoding="UTF-8"?>\n' + item, media_type=XML)

    async def picture(self, request: Request) -> Response:
        sid = int(request.path_params["sid"])
        ch, stream = divmod(sid, 100)
        if not 1 <= ch <= len(self.channels) or stream not in (1, 2):
            return await self._not_found(request, Exception())
        if not self.channels[ch - 1].online:
            return Response(_status(3, "deviceError", "Device Error", request.url.path), 500, media_type=XML)
        return Response(SNAPSHOT, media_type="image/jpeg")


class _AuthMiddleware:
    def __init__(self, app: object, mock: HikvisionMock) -> None:
        self.inner, self.mock = app, mock

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)  # type: ignore[operator]
            return
        request = Request(scope)
        self.mock.requests.append(f"{request.method} {request.url.path}")
        if not self.mock.auth.check(request):
            body = _status(4, "badAuthorization", "Invalid Operation", request.url.path)
            resp = self.mock.auth.challenge(body, XML)
            await resp(scope, receive, send)  # type: ignore[arg-type]
            return
        await self.inner(scope, receive, send)  # type: ignore[operator]
