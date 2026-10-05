"""Reproducción de fixtures capturadas (`vms.vendors.capture`): transporte httpx y servidor RTSP.

- `ReplayTransport(folder, password)` responde a cada petición con la respuesta grabada para el mismo método,
  ruta, query (sin importar el orden) y operación SOAP. Emula la autenticación con los retos grabados
  (anonimizados): sin credenciales → el 401 grabado; con credenciales → comprueba Digest/Basic/WS-Security con
  la contraseña de prueba y, si no cuadra, responde el 401 de contraseña mala grabado (`auth_failure.json`) o
  el reto. Las imágenes (snapshot) son sintéticas: la captura nunca guarda fotos.
- `RtspReplayServer(folder, password)` sirve OPTIONS/DESCRIBE con los retos y el SDP grabados.

Cuenta las peticiones con credenciales para comprobar el «un solo intento».
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

from vms.vendors.auth import parse_challenges, verify_digest
from vms.vendors.capture import soap_operation

SNAPSHOT = (Path(__file__).parent / "assets" / "snapshot.jpg").read_bytes()
_SNAPSHOT_PATHS = re.compile(r"(/picture$|/snapshot\.cgi$|/onvif/snapshot|/snapshot)", re.IGNORECASE)


def _not_authorized() -> httpx.Response:
    return httpx.Response(400, content=b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-'
                                       b'envelope"><s:Body><s:Fault><s:Code><s:Subcode><s:Value>ter:NotAuthorized'
                                       b"</s:Value></s:Subcode></s:Code></s:Fault></s:Body></s:Envelope>",
                          headers={"content-type": "application/soap+xml"})


def _query_key(items: list[tuple[str, str]] | list[list[str]]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in items))


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, folder: Path, password: str, username: str = "admin") -> None:
        self.folder = Path(folder)
        self.password, self.username = password, username
        self.records: list[dict[str, Any]] = [json.loads(p.read_text(encoding="utf-8"))
                                              for p in sorted((self.folder / "http").glob("[0-9]*.json"))]
        fail = self.folder / "http" / "auth_failure.json"
        self.failure: dict[str, Any] | None = json.loads(fail.read_text(encoding="utf-8")) if fail.is_file() else None
        self.challenge = next((r["response"] for r in self.records
                               if r["response"]["status"] == 401 and not r["request"]["authorized"]), None)
        self.realm = ""
        if self.challenge:
            chs = parse_challenges(self.challenge["headers"].get("www-authenticate", []))
            self.realm = chs[0].realm if chs else ""
        self.credentialed = 0
        self.rejected = 0
        self.unmatched: list[str] = []

    @staticmethod
    def _resp(r: dict[str, Any], extra: dict[str, str] | None = None) -> httpx.Response:
        headers = [(k, v) for k, vs in r["headers"].items() if k not in ("content-length", "content-encoding",
                                                                         "transfer-encoding") for v in vs]
        body = SNAPSHOT if r.get("binary") else r["body"].encode("utf-8")
        return httpx.Response(r["status"], headers=headers + list((extra or {}).items()), content=body)

    def _ws_ok(self, body: str) -> bool | None:
        """None = la petición no trae UsernameToken; True/False = trae y la contraseña cuadra o no."""
        user = re.search(r"<(?:[\w.\-]+:)?Username[^>]*>([^<]*)</", body)
        if not user:
            return None
        pwd = re.search(r"<(?:[\w.\-]+:)?Password[^>]*>([^<]*)</", body)
        nonce = re.search(r"<(?:[\w.\-]+:)?Nonce[^>]*>([^<]*)</", body)
        created = re.search(r"<(?:[\w.\-]+:)?Created[^>]*>([^<]*)</", body)
        if not (pwd and nonce and created) or user.group(1) != self.username:
            return False
        digest = base64.b64encode(hashlib.sha1(base64.b64decode(nonce.group(1)) + created.group(1).encode()
                                               + self.password.encode()).digest()).decode()
        return digest == pwd.group(1)

    def _http_ok(self, header: str, method: str) -> bool:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "basic":
            user, _, pw = base64.b64decode(value).decode("utf-8", errors="replace").partition(":")
            return user == self.username and pw == self.password
        return verify_digest(header, method, self.username, self.password, self.realm)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = (await request.aread()).decode("utf-8", errors="replace") if request.method == "POST" else ""
        op = soap_operation(body) if body else ""
        auth = request.headers.get("authorization", "")
        ws = self._ws_ok(body) if body else None
        if auth or ws is not None:
            self.credentialed += 1
        # autenticación HTTP
        if self.challenge is not None and not op:
            if not auth:
                return self._resp(self.challenge)
            if not self._http_ok(auth, request.method):
                self.rejected += 1
                return self._resp(self.failure["response"] if self.failure else self.challenge)
        # autenticación ONVIF (WS-Security)
        if ws is False:
            self.rejected += 1
            if self.failure:
                return self._resp(self.failure["response"])
            return _not_authorized()
        path = request.url.path
        if request.method == "GET" and _SNAPSHOT_PATHS.search(path):
            return httpx.Response(200, content=SNAPSHOT, headers={"content-type": "image/jpeg"})
        query = _query_key(parse_qsl(request.url.query.decode("ascii", errors="replace"), keep_blank_values=True))
        matches = [r for r in self.records
                   if r["request"]["method"] == request.method and r["request"]["path"] == path
                   and r["request"].get("soap_op", "") == op and _query_key(r["request"]["query"]) == query
                   and r["response"]["status"] != 401]
        credentialed = bool(auth) or ws is not None
        same = [r for r in matches if bool(r["request"]["authorized"]) == credentialed]
        if op and not credentialed and matches and not same:
            # operación SOAP que el equipo solo contestó con credenciales: sin ellas, NotAuthorized (como el equipo)
            return _not_authorized()
        for r in same or matches:
            return self._resp(r["response"])
        self.unmatched.append(f"{request.method} {path} {op}".strip())
        return httpx.Response(404, content=b"no grabado")


class RtspReplayServer:
    """OPTIONS y DESCRIBE con los retos y el SDP grabados; comprueba Digest con la contraseña de prueba."""

    def __init__(self, folder: Path, password: str, username: str = "admin") -> None:
        data = json.loads((Path(folder) / "rtsp" / "handshake.json").read_text(encoding="utf-8"))
        self.path: str = data["path"]
        self.steps: list[dict[str, Any]] = data["steps"]
        sdp_file = Path(folder) / "rtsp" / "main_ch1.sdp"
        self.sdp = sdp_file.read_text(encoding="utf-8") if sdp_file.is_file() else ""
        self.password, self.username = password, username
        self.challenge = next((s for s in self.steps if s["status"] == 401 and not s["authorized"]), None)
        chs = parse_challenges(self.challenge["headers"].get("www-authenticate", [])) if self.challenge else []
        self.realms = {c.realm for c in chs}
        self.credentialed = 0
        self.rejected = 0
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def __aenter__(self) -> RtspReplayServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _ok(self, header: str, method: str) -> bool:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "basic":
            user, _, pw = base64.b64decode(value).decode("utf-8", errors="replace").partition(":")
            return user == self.username and pw == self.password
        return any(verify_digest(header, method, self.username, self.password, realm) for realm in self.realms)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("utf-8", errors="replace").split("\r\n")
                method = lines[0].split(" ")[0]
                hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)}
                cseq = hdrs.get("cseq", "0")
                auth = hdrs.get("authorization", "")
                if auth:
                    self.credentialed += 1
                out: list[str]
                body = b""
                if method == "OPTIONS":
                    out = ["RTSP/1.0 200 OK", "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"]
                elif method == "DESCRIBE" and self.challenge is not None and (not auth or not self._ok(auth, method)):
                    if auth:
                        self.rejected += 1
                    out = ["RTSP/1.0 401 Unauthorized"] + [f"WWW-Authenticate: {v}" for v in
                                                           self.challenge["headers"].get("www-authenticate", [])]
                elif method == "DESCRIBE" and self.sdp:
                    body = self.sdp.encode("utf-8")
                    out = ["RTSP/1.0 200 OK", "Content-Type: application/sdp", f"Content-Length: {len(body)}"]
                else:
                    out = ["RTSP/1.0 404 Not Found"]
                writer.write(("\r\n".join([out[0], f"CSeq: {cseq}", *out[1:]]) + "\r\n\r\n").encode("utf-8") + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()


__all__ = ["ReplayTransport", "RtspReplayServer"]
