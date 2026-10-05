"""Mock ASGI de la API HTTP de Dahua: CGI (Digest) y RPC2 (login por desafío).

CGI (texto «clave=valor» separado por CRLF, como el equipo real):
  /cgi-bin/magicBox.cgi?action=getDeviceType | getSystemInfo | getSerialNo | getSoftwareVersion
                        | getMachineName | getProductDefinition&name=MaxRemoteInputChannels|VideoInputChannels
  /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle | Encode | SmartEncode | Network | NTP
                        | Telnet | SSHD | UPnP | T2UServer | Web
  /cgi-bin/configManager.cgi?action=setConfig&Encode[i].ExtraFormat[0].Video.Compression=H.264  («Corregir códec»)
  /cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uniqueChannels[0]=-1   (NVR y XVR: canales IP)
  /cgi-bin/snapshot.cgi?channel=N[&type=1]                                       (JPEG sintético)
  /cgi-bin/global.cgi?action=getCurrentTime                                      (hora local con desfase)
Acción desconocida → 400 «Error\\r\\nBad Request!»; credenciales malas → 401 Digest; tras `lock_after` fallos
con credenciales → 401 con «Error … User Locked» (formato no verificado con hardware).
XVR: `analog` entradas analógicas (canales 1..A) y después los canales IP (`channels`), como en el equipo.

RPC2 (JSON):
  POST /RPC2_Login  global.login en dos pasos. El segundo envía
        password = MD5(user:random:MD5(user:realm:password).upper()).upper()
  POST /RPC2        magicBox.getDeviceType, magicBox.getSerialNo, configManager.getConfig
                    (name=ChannelTitle), global.logout. Requiere «session» válida.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .http_auth import AuthMode, HttpAuthChecker

SNAPSHOT = (Path(__file__).parent / "assets" / "snapshot.jpg").read_bytes()
BAD_REQUEST = "Error\r\nBad Request!\r\n"
LOGIN_CHALLENGE_CODE = 268632079
LOGIN_FAILED_CODE = 268632085
_ENCODE_KEY = re.compile(r"Encode\[(\d+)\]\.(MainFormat|ExtraFormat)\[0\]\.Video\.Compression")


@dataclass
class DahuaChannel:
    name: str
    online: bool = True
    main_codec: str = "H.264"
    sub_codec: str = "H.264"
    main_size: tuple[int, int] = (2688, 1520)
    sub_size: tuple[int, int] = (704, 576)
    main_kbps: int = 4096
    sub_kbps: int = 512
    gop: int = 50
    fps: int = 25
    smart: bool = False


def _md5u(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


@dataclass
class DahuaMock:
    kind: Literal["nvr", "camera", "xvr"] = "nvr"
    username: str = "admin"
    password: str = "Dah#Pass:1@"
    device_type: str = "DHI-NVR4208-8P-4KS2/L"
    serial: str = "6J0123PAZ12345"
    version: str = "4.001.0000000.1,build:2021-06-04"
    channels: list[DahuaChannel] = field(default_factory=lambda: [
        DahuaChannel("Entrada"), DahuaChannel("Cajas"), DahuaChannel("Almacén", online=False),
        DahuaChannel("Pasillo 1", main_codec="H.265"),
    ])
    analog: list[DahuaChannel] = field(default_factory=list)   # solo XVR
    mac: str = "3c:ef:8c:12:34:56"
    auth_mode: AuthMode = "digest"
    lock_after: int | None = 5
    clock_offset_s: float = 0.0
    ntp_enable: bool = True
    security: dict[str, bool] = field(default_factory=lambda: {
        "Telnet": False, "SSHD": False, "UPnP": True, "T2UServer": True})
    requests: list[str] = field(default_factory=list)
    sets: list[str] = field(default_factory=list)
    locked: bool = False

    def __post_init__(self) -> None:
        if self.kind == "camera":
            self.channels = self.channels[:1]
            if self.device_type.startswith("DHI-NVR"):
                self.device_type, self.serial = "IPC-HDW2431T-AS-S2", "7G0456PAZ67890"
        if self.kind == "xvr":
            if self.device_type.startswith("DHI-NVR"):
                self.device_type, self.serial = "DH-XVR5108HS-I3", "8H0789PAZ24680"
            if not self.analog:
                self.analog = [DahuaChannel(f"Analógica {i}", main_size=(1920, 1080)) for i in range(1, 5)]
        self.realm = f"Login to {self.serial}"
        self.auth = HttpAuthChecker(self.username, self.password, realm=self.realm, mode=self.auth_mode)
        self._sessions: set[str] = set()
        self._pending: dict[str, str] = {}
        inner = Starlette(routes=[
            Route("/cgi-bin/magicBox.cgi", self.magicbox),
            Route("/cgi-bin/configManager.cgi", self.config_manager),
            Route("/cgi-bin/LogicDeviceManager.cgi", self.logic_device),
            Route("/cgi-bin/snapshot.cgi", self.snapshot),
            Route("/cgi-bin/global.cgi", self.global_cgi),
            Route("/RPC2_Login", self.rpc_login, methods=["POST"]),
            Route("/RPC2", self.rpc, methods=["POST"]),
        ])
        self.app = _CgiAuth(inner, self)

    @property
    def all_channels(self) -> list[DahuaChannel]:
        return [*self.analog, *self.channels] if self.kind == "xvr" else self.channels

    # ------------------------------------------------------------------ CGI
    def _lines(self, pairs: list[tuple[str, Any]]) -> PlainTextResponse:
        return PlainTextResponse("".join(f"{k}={v}\r\n" for k, v in pairs))

    async def magicbox(self, request: Request) -> Response:
        action = request.query_params.get("action", "")
        name = request.query_params.get("name")
        if action == "getDeviceType":
            return self._lines([("type", self.device_type)])
        if action == "getSerialNo":
            return self._lines([("sn", self.serial)])
        if action == "getSoftwareVersion":
            return self._lines([("version", self.version)])
        if action == "getMachineName":
            return self._lines([("name", self.serial)])
        if action == "getSystemInfo":
            return self._lines([("appAutoStart", "true"), ("deviceType", self.device_type),
                                ("processor", "ST7108"), ("serialNumber", self.serial),
                                ("updateSerial", self.device_type.removeprefix("DHI-"))])
        if action == "getProductDefinition" and name == "MaxRemoteInputChannels":
            return self._lines([("table.MaxRemoteInputChannels", 0 if self.kind == "camera" else len(self.channels))])
        if action == "getProductDefinition" and name == "VideoInputChannels":
            local = len(self.analog) if self.kind == "xvr" else (1 if self.kind == "camera" else 0)
            return self._lines([("table.VideoInputChannels", local)])
        return PlainTextResponse(BAD_REQUEST, status_code=400)

    async def config_manager(self, request: Request) -> Response:
        action = request.query_params.get("action")
        if action == "setConfig":
            return self._set_config(request)
        if action != "getConfig":
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        name = request.query_params.get("name")
        chans = self.all_channels
        if name == "ChannelTitle":
            return self._lines([(f"table.ChannelTitle[{i}].Name", c.name) for i, c in enumerate(chans)])
        if name == "Encode":
            pairs: list[tuple[str, Any]] = []
            for i, c in enumerate(chans):
                for fmt, codec, (w, h), kbps in (("MainFormat", c.main_codec, c.main_size, c.main_kbps),
                                                 ("ExtraFormat", c.sub_codec, c.sub_size, c.sub_kbps)):
                    base = f"table.Encode[{i}].{fmt}[0]"
                    pairs += [(f"{base}.VideoEnable", "true"), (f"{base}.Video.Compression", codec),
                              (f"{base}.Video.Width", w), (f"{base}.Video.Height", h),
                              (f"{base}.Video.FPS", c.fps if fmt == "MainFormat" else 15),
                              (f"{base}.Video.GOP", c.gop if fmt == "MainFormat" else 30),
                              (f"{base}.Video.BitRate", kbps)]
            return self._lines(pairs)
        if name == "SmartEncode":
            return self._lines([(f"table.SmartEncode[{i}].Enable", "true" if c.smart else "false")
                                for i, c in enumerate(chans)])
        if name == "Network":
            return self._lines([("table.Network.eth0.IPAddress", "192.168.1.108"),
                                ("table.Network.eth0.PhysicalAddress", self.mac), ("table.Network.TCPPort", 37777)])
        if name == "NTP":
            return self._lines([("table.NTP.Enable", "true" if self.ntp_enable else "false"),
                                ("table.NTP.Address", "pool.ntp.org"), ("table.NTP.Port", 123)])
        if name in ("Telnet", "SSHD", "UPnP", "T2UServer"):
            return self._lines([(f"table.{name}.Enable", "true" if self.security.get(name) else "false")])
        if name == "Web":
            return self._lines([("table.Web.Enable", "true"), ("table.Web.SSLEnable", "false")])
        return PlainTextResponse(BAD_REQUEST, status_code=400)

    def _set_config(self, request: Request) -> Response:
        changed = False
        for key, value in request.query_params.items():
            if key == "action":
                continue
            self.sets.append(f"{key}={value}")
            m = _ENCODE_KEY.fullmatch(key)
            if not m or int(m.group(1)) >= len(self.all_channels):
                return PlainTextResponse(BAD_REQUEST, status_code=400)
            c = self.all_channels[int(m.group(1))]
            if m.group(2) == "MainFormat":
                c.main_codec = value
            else:
                c.sub_codec = value
            changed = True
        return PlainTextResponse("OK\r\n") if changed else PlainTextResponse(BAD_REQUEST, status_code=400)

    async def global_cgi(self, request: Request) -> Response:
        if request.query_params.get("action") != "getCurrentTime":
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        now = datetime.now().astimezone() + timedelta(seconds=self.clock_offset_s)
        return self._lines([("result", now.strftime("%Y-%m-%d %H:%M:%S"))])

    async def logic_device(self, request: Request) -> Response:
        if self.kind == "camera" or request.query_params.get("action") != "getCameraState":
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        first_ip = len(self.analog) if self.kind == "xvr" else 0
        pairs: list[tuple[str, Any]] = []
        for i, c in enumerate(self.channels):
            pairs += [(f"states[{i}].channel", first_ip + i),
                      (f"states[{i}].connectionState", "Connected" if c.online else "Unconnect")]
        return self._lines(pairs)

    async def snapshot(self, request: Request) -> Response:
        try:
            ch = int(request.query_params.get("channel", "1"))
        except ValueError:
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        if not 1 <= ch <= len(self.all_channels):
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        if not self.all_channels[ch - 1].online:
            return PlainTextResponse("Error\r\nInternal Error\r\n", status_code=500)
        return Response(SNAPSHOT, media_type="image/jpeg")

    # ------------------------------------------------------------------ RPC2
    async def rpc_login(self, request: Request) -> Response:
        try:
            body = json.loads(await request.body())
            params = body.get("params") or {}
            req_id = body.get("id", 1)
        except (ValueError, AttributeError):
            return JSONResponse({"error": {"code": 287637505, "message": "Invalid request"}, "result": False})
        user = params.get("userName", "")
        session = body.get("session")
        if not params.get("password") or session not in self._pending:
            session = secrets.token_hex(16)
            random = str(secrets.randbelow(10**9))
            self._pending[session] = random
            return JSONResponse({"error": {"code": LOGIN_CHALLENGE_CODE, "message": "Component error: login challenge!"},
                                 "id": req_id, "params": {"authorization": secrets.token_hex(20),
                                                          "encryption": "Default", "mac": "3CEF8C123456",
                                                          "random": random, "realm": self.realm},
                                 "result": False, "session": session})
        random = self._pending.pop(session)
        expected = _md5u(f"{self.username}:{random}:{_md5u(f'{self.username}:{self.realm}:{self.password}')}")
        if user != self.username or params.get("password") != expected:
            return JSONResponse({"error": {"code": LOGIN_FAILED_CODE, "message": "Component error: password not valid!"},
                                 "id": req_id, "params": {"remainLockTimes": 4}, "result": False, "session": session})
        self._sessions.add(session)
        return JSONResponse({"id": req_id, "params": {"keepAliveInterval": 60}, "result": True, "session": session})

    async def rpc(self, request: Request) -> Response:
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"error": {"code": 287637505, "message": "Invalid request"}, "result": False})
        session, method, req_id = body.get("session"), body.get("method"), body.get("id", 1)
        if session not in self._sessions:
            return JSONResponse({"error": {"code": 287637504, "message": "Invalid session in request data!"},
                                 "id": req_id, "result": False, "session": session})
        result: dict[str, Any] | None = None
        if method == "magicBox.getDeviceType":
            result = {"type": self.device_type}
        elif method == "magicBox.getSerialNo":
            result = {"sn": self.serial}
        elif method == "configManager.getConfig" and (body.get("params") or {}).get("name") == "ChannelTitle":
            result = {"table": [{"Name": c.name} for c in self.all_channels]}
        elif method == "global.logout":
            self._sessions.discard(session)
            return JSONResponse({"id": req_id, "params": None, "result": True, "session": session})
        if result is None:
            return JSONResponse({"error": {"code": 268894210, "message": "Method not found"},
                                 "id": req_id, "result": False, "session": session})
        return JSONResponse({"id": req_id, "params": result, "result": True, "session": session})


class _CgiAuth:
    """Digest solo para /cgi-bin; RPC2 lleva su propia autenticación en el cuerpo."""

    def __init__(self, app: Any, mock: DahuaMock) -> None:
        self.inner, self.mock = app, mock

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            self.mock.requests.append(f"{request.method} {request.url.path}?{request.url.query}")
            if request.url.path.startswith("/cgi-bin/"):
                has_auth = bool(request.headers.get("authorization"))
                ok = self.mock.auth.check(request) and not (self.mock.locked and has_auth)
                if not ok:
                    if has_auth and self.mock.lock_after and \
                            self.mock.auth.rejected_credentials >= self.mock.lock_after:
                        self.mock.locked = True
                    body = ("Error\r\nUser Locked! Please try again after 30 minutes.\r\n" if self.mock.locked
                            else "Error\r\nInvalid Authority!\r\n")
                    await self.mock.auth.challenge(body)(scope, receive, send)
                    return
        await self.inner(scope, receive, send)
