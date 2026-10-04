"""Mock ASGI de la API HTTP de Dahua: CGI (Digest) y RPC2 (login por desafío).

CGI (texto «clave=valor» separado por CRLF, como el equipo real):
  /cgi-bin/magicBox.cgi?action=getDeviceType | getSystemInfo | getSerialNo | getSoftwareVersion
                        | getMachineName | getProductDefinition&name=MaxRemoteInputChannels
  /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle | Encode
  /cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uniqueChannels[0]=-1   (solo NVR)
  /cgi-bin/snapshot.cgi?channel=N                                                (JPEG sintético)
Acción desconocida → 400 «Error\\r\\nBad Request!»; credenciales malas → 401 Digest.

RPC2 (JSON):
  POST /RPC2_Login  global.login en dos pasos. El segundo envía
        password = MD5(user:random:MD5(user:realm:password).upper()).upper()
  POST /RPC2        magicBox.getDeviceType, magicBox.getSerialNo, configManager.getConfig
                    (name=ChannelTitle), global.logout. Requiere «session» válida.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .http_auth import HttpAuthChecker

SNAPSHOT = (Path(__file__).parent / "assets" / "snapshot.jpg").read_bytes()
BAD_REQUEST = "Error\r\nBad Request!\r\n"
LOGIN_CHALLENGE_CODE = 268632079
LOGIN_FAILED_CODE = 268632085


@dataclass
class DahuaChannel:
    name: str
    online: bool = True
    main_codec: str = "H.264"
    sub_codec: str = "H.264"
    main_size: tuple[int, int] = (2688, 1520)
    sub_size: tuple[int, int] = (704, 576)


def _md5u(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


@dataclass
class DahuaMock:
    kind: Literal["nvr", "camera"] = "nvr"
    username: str = "admin"
    password: str = "Dah#Pass:1@"
    device_type: str = "DHI-NVR4208-8P-4KS2/L"
    serial: str = "6J0123PAZ12345"
    version: str = "4.001.0000000.1,build:2021-06-04"
    channels: list[DahuaChannel] = field(default_factory=lambda: [
        DahuaChannel("Entrada"), DahuaChannel("Cajas"), DahuaChannel("Almacén", online=False),
        DahuaChannel("Pasillo 1", main_codec="H.265"),
    ])
    requests: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind == "camera":
            self.channels = self.channels[:1]
            if self.device_type.startswith("DHI-NVR"):
                self.device_type, self.serial = "IPC-HDW2431T-AS-S2", "7G0456PAZ67890"
        self.realm = f"Login to {self.serial}"
        self.auth = HttpAuthChecker(self.username, self.password, realm=self.realm, mode="digest")
        self._random = ""
        self._sessions: set[str] = set()
        self._pending: dict[str, str] = {}
        inner = Starlette(routes=[
            Route("/cgi-bin/magicBox.cgi", self.magicbox),
            Route("/cgi-bin/configManager.cgi", self.config_manager),
            Route("/cgi-bin/LogicDeviceManager.cgi", self.logic_device),
            Route("/cgi-bin/snapshot.cgi", self.snapshot),
            Route("/RPC2_Login", self.rpc_login, methods=["POST"]),
            Route("/RPC2", self.rpc, methods=["POST"]),
        ])
        self.app = _CgiAuth(inner, self)

    # ------------------------------------------------------------------ CGI
    def _lines(self, pairs: list[tuple[str, Any]]) -> PlainTextResponse:
        return PlainTextResponse("".join(f"{k}={v}\r\n" for k, v in pairs))

    async def magicbox(self, request: Request) -> Response:
        action = request.query_params.get("action", "")
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
        if action == "getProductDefinition" and request.query_params.get("name") == "MaxRemoteInputChannels":
            return self._lines([("table.MaxRemoteInputChannels", len(self.channels))])
        return PlainTextResponse(BAD_REQUEST, status_code=400)

    async def config_manager(self, request: Request) -> Response:
        if request.query_params.get("action") != "getConfig":
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        name = request.query_params.get("name")
        if name == "ChannelTitle":
            return self._lines([(f"table.ChannelTitle[{i}].Name", c.name) for i, c in enumerate(self.channels)])
        if name == "Encode":
            pairs: list[tuple[str, Any]] = []
            for i, c in enumerate(self.channels):
                for fmt, codec, (w, h) in (("MainFormat", c.main_codec, c.main_size),
                                           ("ExtraFormat", c.sub_codec, c.sub_size)):
                    base = f"table.Encode[{i}].{fmt}[0]"
                    pairs += [(f"{base}.VideoEnable", "true"), (f"{base}.Video.Compression", codec),
                              (f"{base}.Video.Width", w), (f"{base}.Video.Height", h),
                              (f"{base}.Video.FPS", 25 if fmt == "MainFormat" else 15),
                              (f"{base}.Video.BitRate", 4096 if fmt == "MainFormat" else 512)]
            return self._lines(pairs)
        return PlainTextResponse(BAD_REQUEST, status_code=400)

    async def logic_device(self, request: Request) -> Response:
        if self.kind != "nvr" or request.query_params.get("action") != "getCameraState":
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        pairs: list[tuple[str, Any]] = []
        for i, c in enumerate(self.channels):
            pairs += [(f"states[{i}].channel", i),
                      (f"states[{i}].connectionState", "Connected" if c.online else "Unconnect")]
        return self._lines(pairs)

    async def snapshot(self, request: Request) -> Response:
        try:
            ch = int(request.query_params.get("channel", "1"))
        except ValueError:
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        if not 1 <= ch <= len(self.channels):
            return PlainTextResponse(BAD_REQUEST, status_code=400)
        if not self.channels[ch - 1].online:
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
            result = {"table": [{"Name": c.name} for c in self.channels]}
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

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            self.mock.requests.append(f"{request.method} {request.url.path}?{request.url.query}")
            if request.url.path.startswith("/cgi-bin/") and not self.mock.auth.check(request):
                await self.mock.auth.challenge("Error\r\nInvalid Authority!\r\n")(scope, receive, send)
                return
        await self.inner(scope, receive, send)
