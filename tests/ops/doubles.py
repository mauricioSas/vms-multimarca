"""Dobles de prueba de B6 (CONTRATO §18.19): mientras B5 no entregue `time_read` y `security_read`.

- `IsapiClock`, `DahuaClock`, `OnvifClock`: implementan `DeviceClockClient.device_time()` leyendo respuestas
  con el formato real de cada marca (XML de ISAPI, texto de CGI, SOAP de ONVIF) servidas por apps ASGI
  locales (`httpx.ASGITransport`, sin red). Miden la ida y vuelta y calculan la hora del PC a mitad del viaje.
- `RtspDouble`: servidor RTSP mínimo (OPTIONS/DESCRIBE con Digest o sin autenticación) que cuenta cuántas
  peticiones llevan credenciales.
- `SmtpDouble`: servidor SMTP mínimo (sin TLS) que guarda los mensajes recibidos.
- `DiagClient`: cliente de equipo con contador de intentos con credenciales.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from vms.core.errors import DeviceAuthFailed, DeviceUnreachable
from vms.core.interfaces import ChannelInfo, DeviceInfo, DeviceSecuritySettings, DeviceTime


# =========================================================================== hora
@dataclass
class ClockDevice:
    """Equipo simulado: su reloj va `skew` por delante del PC y está en NTP o manual."""

    skew: timedelta = timedelta(0)
    mode: Literal["ntp", "manual"] = "ntp"
    tz: str = "Europe/Madrid"

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.skew


def isapi_app(dev: ClockDevice) -> Starlette:
    async def time_(request: Request) -> Response:
        local = dev.now().astimezone(ZoneInfo(dev.tz))
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
               f"<timeMode>{'NTP' if dev.mode == 'ntp' else 'manual'}</timeMode>"
               f"<localTime>{local.isoformat(timespec='seconds')}</localTime>"
               "<timeZone>CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00</timeZone></Time>")
        return Response(xml, media_type="application/xml")
    return Starlette(routes=[Route("/ISAPI/System/time", time_)])


def dahua_app(dev: ClockDevice) -> Starlette:
    async def global_(request: Request) -> Response:
        if request.query_params.get("action") != "getCurrentTime":
            return PlainTextResponse("Error", 400)
        local = dev.now().astimezone(ZoneInfo(dev.tz))
        return PlainTextResponse(f"result={local.strftime('%Y-%m-%d %H:%M:%S')}\r\n")

    async def config(request: Request) -> Response:
        if request.query_params.get("name") != "NTP":
            return PlainTextResponse("Error", 400)
        return PlainTextResponse(f"table.NTP.Address=192.168.1.10\r\ntable.NTP.Enable={'true' if dev.mode == 'ntp' else 'false'}\r\n")
    return Starlette(routes=[Route("/cgi-bin/global.cgi", global_), Route("/cgi-bin/configManager.cgi", config)])


def onvif_app(dev: ClockDevice) -> Starlette:
    async def device(request: Request) -> Response:
        body = (await request.body()).decode()
        if "GetSystemDateAndTime" not in body:
            return Response("", 400)
        u = dev.now().astimezone(timezone.utc)
        xml = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
               'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>'
               "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
               f"<tt:DateTimeType>{'NTP' if dev.mode == 'ntp' else 'Manual'}</tt:DateTimeType>"
               "<tt:DaylightSavings>true</tt:DaylightSavings><tt:UTCDateTime>"
               f"<tt:Time><tt:Hour>{u.hour}</tt:Hour><tt:Minute>{u.minute}</tt:Minute><tt:Second>{u.second}</tt:Second></tt:Time>"
               f"<tt:Date><tt:Year>{u.year}</tt:Year><tt:Month>{u.month}</tt:Month><tt:Day>{u.day}</tt:Day></tt:Date>"
               "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body></s:Envelope>")
        return Response(xml, media_type="application/soap+xml")
    return Starlette(routes=[Route("/onvif/device_service", device, methods=["POST"])])


class _ClockBase:
    vendor = "test"

    def __init__(self, app: Starlette, tz: str = "Europe/Madrid") -> None:
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://device.local")
        self.tz = tz

    async def probe(self) -> DeviceInfo:
        return DeviceInfo(vendor="hikvision", model="TEST")

    async def list_channels(self) -> list[ChannelInfo]:
        return []

    async def snapshot(self, channel: int, stream: str = "main") -> bytes:
        raise DeviceUnreachable("sin imagen")

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _timed(self, coro: Any) -> tuple[Any, datetime, float]:
        t0 = datetime.now(timezone.utc)
        r = await coro
        t1 = datetime.now(timezone.utc)
        return r, t0 + (t1 - t0) / 2, (t1 - t0).total_seconds() * 1000


class IsapiClock(_ClockBase):
    vendor = "hikvision"

    async def device_time(self) -> DeviceTime:
        r, mid, rtt = await self._timed(self.http.get("/ISAPI/System/time"))
        local = re.search(r"<localTime>([^<]+)</localTime>", r.text).group(1)  # type: ignore[union-attr]
        mode = re.search(r"<timeMode>([^<]+)</timeMode>", r.text).group(1)  # type: ignore[union-attr]
        return DeviceTime(device_time=datetime.fromisoformat(local), measured_at=mid, round_trip_ms=rtt,
                          time_mode="ntp" if mode.upper() == "NTP" else "manual", source="isapi")


class DahuaClock(_ClockBase):
    vendor = "dahua"

    async def device_time(self) -> DeviceTime:
        r, mid, rtt = await self._timed(self.http.get("/cgi-bin/global.cgi", params={"action": "getCurrentTime"}))
        value = r.text.strip().split("=", 1)[1]
        local = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(self.tz))
        ntp = await self.http.get("/cgi-bin/configManager.cgi", params={"action": "getConfig", "name": "NTP"})
        enabled = "table.NTP.Enable=true" in ntp.text
        return DeviceTime(device_time=local, measured_at=mid, round_trip_ms=rtt,
                          time_mode="ntp" if enabled else "manual", source="cgi")


class OnvifClock(_ClockBase):
    vendor = "onvif"
    BODY = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>')

    async def device_time(self) -> DeviceTime:
        r, mid, rtt = await self._timed(self.http.post("/onvif/device_service", content=self.BODY))

        def g(tag: str) -> int:
            return int(re.search(rf"<tt:{tag}>(\d+)</tt:{tag}>", r.text).group(1))  # type: ignore[union-attr]
        dt = datetime(g("Year"), g("Month"), g("Day"), g("Hour"), g("Minute"), g("Second"), tzinfo=timezone.utc)
        mode = re.search(r"<tt:DateTimeType>(\w+)</tt:DateTimeType>", r.text).group(1)  # type: ignore[union-attr]
        return DeviceTime(device_time=dt, measured_at=mid, round_trip_ms=rtt,
                          time_mode="ntp" if mode == "NTP" else "manual", source="onvif")


# =========================================================================== cliente de diagnóstico
@dataclass
class DiagBehavior:
    reachable: bool = True
    password: str = "Buena#1234"
    attempts: int = 0                 # intentos con credenciales
    security: DeviceSecuritySettings | None = None


class DiagClient:
    """Cliente de equipo que cuenta los intentos con credenciales (como el contador de un mock)."""

    def __init__(self, behavior: DiagBehavior, password: str, vendor: str = "hikvision") -> None:
        self.b = behavior
        self.password = password
        self.vendor = vendor

    async def probe(self) -> DeviceInfo:
        if not self.b.reachable:
            raise DeviceUnreachable("El equipo no responde (tiempo agotado)")
        self.b.attempts += 1
        if self.password != self.b.password:
            raise DeviceAuthFailed("Usuario o contraseña incorrectos")
        return DeviceInfo(vendor=self.vendor, model="DS-2CD2143G2-I", firmware="V5.7.3 build 220112")  # type: ignore[arg-type]

    async def list_channels(self) -> list[ChannelInfo]:
        return []

    async def snapshot(self, channel: int, stream: str = "main") -> bytes:
        raise DeviceUnreachable("sin imagen")

    async def security_settings(self, admin_username: str, admin_password: str) -> DeviceSecuritySettings:
        self.b.attempts += 1
        return self.b.security or DeviceSecuritySettings()

    async def aclose(self) -> None:
        return None


# =========================================================================== RTSP
@dataclass
class RtspDouble:
    """OPTIONS + DESCRIBE. `auth`: «digest» (pide contraseña) o «none» (vídeo anónimo)."""

    auth: Literal["digest", "none"] = "digest"
    username: str = "admin"
    password: str = "Buena#1234"
    paths: dict[str, str] = field(default_factory=lambda: {"/Streaming/Channels/101": "H264",
                                                           "/Streaming/Channels/102": "H264"})
    credentialed: int = 0
    requests: list[str] = field(default_factory=list)
    port: int = 0
    _server: asyncio.base_events.Server | None = None

    async def start(self) -> "RtspDouble":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonce = secrets.token_hex(8)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, url, _ = lines[0].split(" ", 2)
                headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
                cseq = headers.get("cseq", "1")
                self.requests.append(method)
                if method == "OPTIONS":
                    writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: OPTIONS, DESCRIBE\r\n\r\n".encode())
                    await writer.drain()
                    continue
                path = "/" + url.split("://", 1)[1].split("/", 1)[1] if url.count("/") >= 3 else "/"
                auth = headers.get("authorization", "")
                if auth:
                    self.credentialed += 1
                if self.auth == "digest" and not self._ok(auth, method, url):
                    writer.write((f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: Digest "
                                  f'realm="IP Camera", nonce="{nonce}"\r\n\r\n').encode())
                    await writer.drain()
                    continue
                codec = self.paths.get(path)
                if codec is None:
                    writer.write(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n".encode())
                    await writer.drain()
                    continue
                sdp = f"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 {codec}/90000\r\n"
                writer.write((f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\n"
                              f"Content-Length: {len(sdp)}\r\n\r\n{sdp}").encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, IndexError):
            pass
        finally:
            writer.close()

    def _ok(self, auth: str, method: str, uri: str) -> bool:
        if not auth.lower().startswith("digest "):
            return False
        p = dict(re.findall(r'(\w+)="?([^",]*)"?', auth[7:]))
        ha1 = hashlib.md5(f"{self.username}:{p.get('realm', '')}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{p.get('uri', uri)}".encode()).hexdigest()
        expected = hashlib.md5(f"{ha1}:{p.get('nonce', '')}:{ha2}".encode()).hexdigest()
        return p.get("username") == self.username and p.get("response") == expected


# =========================================================================== SMTP
@dataclass
class SmtpDouble:
    messages: list[dict[str, Any]] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    port: int = 0
    _server: asyncio.base_events.Server | None = None

    async def start(self) -> "SmtpDouble":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import base64

        def send(line: str) -> None:
            writer.write((line + "\r\n").encode())

        send("220 smtp.prueba ESMTP")
        await writer.drain()
        msg: dict[str, Any] = {"rcpt": []}
        try:
            while True:
                line = (await reader.readline()).decode().rstrip("\r\n")
                if not line:
                    break
                cmd = line.split(" ", 1)[0].upper()
                if cmd in ("EHLO", "HELO"):
                    writer.write(b"250-smtp.prueba\r\n250 AUTH PLAIN LOGIN\r\n")
                elif cmd == "AUTH":
                    parts = line.split(" ")
                    if parts[1].upper() == "PLAIN" and len(parts) > 2:
                        _, user, pw = base64.b64decode(parts[2]).decode().split("\0")
                        self.logins.append((user, pw))
                    send("235 Autenticado")
                elif cmd == "MAIL":
                    msg = {"from": line.split(":", 1)[1].strip(" <>"), "rcpt": []}
                    send("250 OK")
                elif cmd == "RCPT":
                    msg["rcpt"].append(line.split(":", 1)[1].strip(" <>"))
                    send("250 OK")
                elif cmd == "DATA":
                    send("354 Adelante")
                    await writer.drain()
                    data = []
                    while True:
                        dl = (await reader.readline()).decode()
                        if dl.rstrip("\r\n") == ".":
                            break
                        data.append(dl)
                    msg["data"] = "".join(data)
                    self.messages.append(msg)
                    send("250 Recibido")
                elif cmd == "QUIT":
                    send("221 Adiós")
                    await writer.drain()
                    break
                else:
                    send("250 OK")
                await writer.drain()
        finally:
            writer.close()
