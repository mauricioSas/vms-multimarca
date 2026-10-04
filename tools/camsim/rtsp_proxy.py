"""Proxy RTSP (solo TCP) que imita las rutas nativas de un equipo Hikvision o Dahua.

Por qué existe: MediaMTX descarta la query string al resolver la ruta, así que
«/cam/realmonitor?channel=1&subtype=0» y «...channel=2...» caen en la MISMA ruta y no se
puede simular un NVR Dahua directamente. Cada equipo simulado escucha en su propio puerto
con este proxy, que:
  - reescribe las URLs de las peticiones (ruta nativa → ruta interna «sim/<equipo>/chN/main|sub»)
    y las de las respuestas (Content-Base, Content-Location, RTP-Info, SDP) en sentido inverso;
  - exige autenticación Digest o Basic como un equipo real (OPTIONS va sin autenticar);
  - deja pasar intactos los paquetes intercalados ($ + canal + longitud) de RTP/RTCP;
  - se puede «desenchufar» (offline) para probar la reconexión.
Solo para pruebas: no forma parte del producto.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable, Literal
from urllib.parse import parse_qs

log = logging.getLogger(__name__)

AuthMode = Literal["digest", "basic", "none"]
_URL_RE = re.compile(r"rtsp://[^\s\"'<>;,]+", re.IGNORECASE)
_DIGEST_PARAM_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]*))')


# --------------------------------------------------------------------------- mapeo de rutas
@dataclass
class PathMapper:
    """Traduce entre la ruta que ve el cliente y la ruta interna del MediaMTX simulador."""

    to_internal: Callable[[str], str]
    to_external: Callable[[str], str]


def _internal(device: str, channel: int, stream: str, rest: str = "") -> str:
    return f"/sim/{device}/ch{channel}/{stream}{rest}"


def _parse_internal(device: str, path: str) -> tuple[int, str, str] | None:
    m = re.match(rf"^/sim/{re.escape(device)}/ch(\d+)/(main|sub)(?P<rest>/.*)?$", path)
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group("rest") or ""


def hikvision_mapper(device: str) -> PathMapper:
    rx = re.compile(r"^/Streaming/Channels/(\d+)(0[12])(?P<rest>/.*)?$", re.IGNORECASE)

    def to_internal(path: str) -> str:
        m = rx.match(path)
        if not m or int(m.group(1)) < 1:
            return f"/sim/{device}/notfound"
        stream = "main" if m.group(2) == "01" else "sub"
        return _internal(device, int(m.group(1)), stream, m.group("rest") or "")

    def to_external(path: str) -> str:
        p = _parse_internal(device, path)
        if not p:
            return path
        ch, stream, rest = p
        return f"/Streaming/Channels/{ch}{'01' if stream == 'main' else '02'}{rest}"

    return PathMapper(to_internal, to_external)


def dahua_mapper(device: str) -> PathMapper:
    rx = re.compile(r"^/cam/realmonitor\?(?P<q>[^/]*)(?P<rest>/.*)?$", re.IGNORECASE)

    def to_internal(path: str) -> str:
        m = rx.match(path)
        if not m:
            return f"/sim/{device}/notfound"
        q = parse_qs(m.group("q"))
        try:
            ch = int(q.get("channel", ["1"])[0])
            subtype = int(q.get("subtype", ["0"])[0])
        except ValueError:
            return f"/sim/{device}/notfound"
        if ch < 1 or subtype not in (0, 1):
            return f"/sim/{device}/notfound"
        return _internal(device, ch, "main" if subtype == 0 else "sub", m.group("rest") or "")

    def to_external(path: str) -> str:
        p = _parse_internal(device, path)
        if not p:
            return path
        ch, stream, rest = p
        return f"/cam/realmonitor?channel={ch}&subtype={0 if stream == 'main' else 1}{rest}"

    return PathMapper(to_internal, to_external)


def generic_mapper(device: str) -> PathMapper:
    """Equipo genérico: /chN/main y /chN/sub."""
    rx = re.compile(r"^/ch(\d+)/(main|sub)(?P<rest>/.*)?$")

    def to_internal(path: str) -> str:
        m = rx.match(path)
        if not m:
            return f"/sim/{device}/notfound"
        return _internal(device, int(m.group(1)), m.group(2), m.group("rest") or "")

    def to_external(path: str) -> str:
        p = _parse_internal(device, path)
        if not p:
            return path
        ch, stream, rest = p
        return f"/ch{ch}/{stream}{rest}"

    return PathMapper(to_internal, to_external)


# --------------------------------------------------------------------------- mensajes RTSP
@dataclass
class RtspMessage:
    first_line: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def set_header(self, name: str, value: str | None) -> None:
        self.headers = [(k, v) for k, v in self.headers if k.lower() != name.lower()]
        if value is not None:
            self.headers.append((name, value))

    def encode(self) -> bytes:
        if self.body:
            self.set_header("Content-Length", str(len(self.body)))
        lines = [self.first_line] + [f"{k}: {v}" for k, v in self.headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + self.body


async def _read_unit(reader: asyncio.StreamReader) -> bytes | RtspMessage | None:
    """Lee un paquete intercalado ($...) o un mensaje RTSP completo. None = conexión cerrada."""
    try:
        first = await reader.readexactly(1)
    except asyncio.IncompleteReadError:
        return None
    if first == b"$":
        hdr = await reader.readexactly(3)
        length = int.from_bytes(hdr[1:3], "big")
        return first + hdr + await reader.readexactly(length)
    head = first + await reader.readuntil(b"\r\n\r\n")
    text = head.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    msg = RtspMessage(lines[0])
    for line in lines[1:]:
        if not line:
            continue
        k, _, v = line.partition(":")
        msg.headers.append((k.strip(), v.strip()))
    length = int(msg.header("Content-Length") or 0)
    if length:
        msg.body = await reader.readexactly(length)
    return msg


# --------------------------------------------------------------------------- proxy
class RtspRewriteProxy:
    def __init__(self, listen_host: str, listen_port: int, upstream_host: str, upstream_port: int,
                 mapper: PathMapper, *, username: str = "", password: str = "",
                 auth: AuthMode = "digest", realm: str = "IP Camera(SIM01)") -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.mapper = mapper
        self.username = username
        self.password = password
        self.auth: AuthMode = auth if username else "none"
        self.realm = realm
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.StreamWriter] = set()
        self._nonces: set[str] = set()
        self.connections_total = 0

    # ---- ciclo de vida ----------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.listen_host, self.listen_port)
        sock = self._server.sockets[0]
        self.listen_port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for w in list(self._conns):
            w.close()
        self._conns.clear()

    @property
    def running(self) -> bool:
        return self._server is not None

    # ---- reescritura -------------------------------------------------------
    def _map_text(self, text: str, inbound: bool, client_authority: str) -> str:
        def repl(m: re.Match[str]) -> str:
            url = m.group(0)
            rest = url[len("rtsp://"):]
            authority, slash, pathq = rest.partition("/")
            pathq = slash + pathq
            if inbound:
                return f"rtsp://{self.upstream_host}:{self.upstream_port}{self.mapper.to_internal(pathq)}"
            return f"rtsp://{client_authority}{self.mapper.to_external(pathq)}"
        return _URL_RE.sub(repl, text)

    def _rewrite(self, msg: RtspMessage, inbound: bool, client_authority: str) -> RtspMessage:
        msg.first_line = self._map_text(msg.first_line, inbound, client_authority)
        msg.headers = [(k, self._map_text(v, inbound, client_authority)) for k, v in msg.headers]
        if msg.body:
            body = msg.body.decode("utf-8", errors="surrogateescape")
            msg.body = self._map_text(body, inbound, client_authority).encode("utf-8", errors="surrogateescape")
        return msg

    # ---- autenticación -----------------------------------------------------
    def _challenge(self, cseq: str | None) -> bytes:
        res = RtspMessage("RTSP/1.0 401 Unauthorized")
        if cseq:
            res.headers.append(("CSeq", cseq))
        if self.auth == "digest":
            nonce = secrets.token_hex(16)
            self._nonces.add(nonce)
            res.headers.append(("WWW-Authenticate", f'Digest realm="{self.realm}", nonce="{nonce}"'))
        else:
            res.headers.append(("WWW-Authenticate", f'Basic realm="{self.realm}"'))
        return res.encode()

    def _authorized(self, msg: RtspMessage) -> bool:
        if self.auth == "none":
            return True
        method = msg.first_line.split(" ", 1)[0].upper()
        if method == "OPTIONS":
            return True
        header = msg.header("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if self.auth == "basic":
            if scheme.lower() != "basic":
                return False
            try:
                user, _, pw = base64.b64decode(value).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return hmac.compare_digest(user, self.username) and hmac.compare_digest(pw, self.password)
        if scheme.lower() != "digest":
            return False
        params = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
                  for m in _DIGEST_PARAM_RE.finditer(value)}
        if params.get("username") != self.username or params.get("nonce") not in self._nonces:
            return False
        uri = params.get("uri", "")
        ha1 = hashlib.md5(f"{self.username}:{self.realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if params.get("qop"):
            expected = hashlib.md5(
                f"{ha1}:{params['nonce']}:{params.get('nc', '')}:{params.get('cnonce', '')}:{params['qop']}:{ha2}"
                .encode()).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{params['nonce']}:{ha2}".encode()).hexdigest()
        return hmac.compare_digest(expected, params.get("response", ""))

    # ---- conexiones --------------------------------------------------------
    async def _handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        self.connections_total += 1
        self._conns.add(c_writer)
        try:
            u_reader, u_writer = await asyncio.open_connection(self.upstream_host, self.upstream_port)
        except OSError as exc:
            log.warning("Simulador: no se pudo conectar con el MediaMTX interno: %s", exc)
            c_writer.close()
            self._conns.discard(c_writer)
            return
        self._conns.add(u_writer)
        state = {"authority": f"{self.listen_host}:{self.listen_port}"}

        async def client_to_upstream() -> None:
            while True:
                unit = await _read_unit(c_reader)
                if unit is None:
                    break
                if isinstance(unit, bytes):
                    u_writer.write(unit)
                else:
                    m = re.match(r"^\S+\s+rtsp://([^/\s]+)", unit.first_line, re.IGNORECASE)
                    if m:
                        state["authority"] = m.group(1).rpartition("@")[2]
                    if not self._authorized(unit):
                        c_writer.write(self._challenge(unit.header("CSeq")))
                        await c_writer.drain()
                        continue
                    unit.set_header("Authorization", None)
                    u_writer.write(self._rewrite(unit, True, state["authority"]).encode())
                await u_writer.drain()

        async def upstream_to_client() -> None:
            while True:
                unit = await _read_unit(u_reader)
                if unit is None:
                    break
                if isinstance(unit, bytes):
                    c_writer.write(unit)
                else:
                    c_writer.write(self._rewrite(unit, False, state["authority"]).encode())
                await c_writer.drain()

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (ConnectionError, asyncio.IncompleteReadError)):
                    log.debug("Simulador: conexión cerrada con error: %r", exc)
        finally:
            for w in (c_writer, u_writer):
                w.close()
                self._conns.discard(w)
