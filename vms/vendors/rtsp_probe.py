"""Prueba RTSP sin ffmpeg: OPTIONS + DESCRIBE sobre TCP con autenticación Digest o Basic.

Sirve para «probar conexión» antes de dar de alta un equipo y para saber el códec del flujo
(H.264/H.265) leyendo el SDP. Implementación mínima de RFC 2326 / RFC 7616 (MD5); no abre
sesiones de vídeo (no hace SETUP/PLAY), así no consume una conexión de streaming del equipo.

Una sola ronda de desafío: si la contraseña es mala devuelve status 401 y NO reintenta
(los equipos bloquean el usuario tras varios fallos).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field

from vms.core import rtsp

log = logging.getLogger("vms.vendors.rtsp")

_PARAM_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]*))')
_RTPMAP_RE = re.compile(r"^a=rtpmap:\d+\s+([A-Za-z0-9\-]+)/", re.MULTILINE)
MAX_HEADER = 64 * 1024


@dataclass
class RtspProbeResult:
    reachable: bool = False
    status: int | None = None
    auth_ok: bool | None = None
    codecs: list[str] = field(default_factory=list)   # ["H264"], ["H265", "MPEG4-GENERIC"]...
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def video_codec(self) -> str | None:
        for c in self.codecs:
            if c.upper() in ("H264", "H265", "HEVC", "MJPEG", "JPEG", "MP4V-ES"):
                return "H.265" if c.upper() in ("H265", "HEVC") else ("H.264" if c.upper() == "H264" else c)
        return None


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _auth_header(challenge: str, method: str, uri: str, username: str, password: str, nc: int) -> str | None:
    scheme, _, rest = challenge.partition(" ")
    if scheme.lower() == "basic":
        import base64
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    if scheme.lower() != "digest":
        return None
    p = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _PARAM_RE.finditer(rest)}
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    qop = p.get("qop", "")
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in [q.strip() for q in qop.split(",")]:
        cnonce = secrets.token_hex(8)
        ncs = f"{nc:08x}"
        response = _md5(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        parts += [f'response="{response}"', "qop=auth", f"nc={ncs}", f'cnonce="{cnonce}"']
    else:
        parts.append(f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"')
    if p.get("opaque"):
        parts.append(f'opaque="{p["opaque"]}"')
    parts.append("algorithm=MD5")
    return "Digest " + ", ".join(parts)


async def _read_response(reader: asyncio.StreamReader) -> tuple[int, dict[str, list[str]], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > MAX_HEADER:
        raise ValueError("cabecera RTSP demasiado grande")
    lines = head.decode("utf-8", errors="replace").split("\r\n")
    status_line = lines[0].split(" ", 2)
    if len(status_line) < 2 or not status_line[0].startswith("RTSP/"):
        raise ValueError("respuesta que no es RTSP")
    status = int(status_line[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.setdefault(k.strip().lower(), []).append(v.strip())
    length = int((headers.get("content-length") or ["0"])[0] or 0)
    body = await reader.readexactly(min(length, MAX_HEADER)) if length else b""
    return status, headers, body


async def probe_rtsp(host: str, port: int, path: str, username: str = "", password: str = "", *,
                     timeout: float = 5.0) -> RtspProbeResult:
    """Hace OPTIONS + DESCRIBE a rtsp://host:port/path. Nunca lanza; devuelve el diagnóstico."""
    result = RtspProbeResult()
    url = f"rtsp://{rtsp.format_host(host)}:{int(port)}{rtsp.normalize_path(path) or '/'}"
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port)), timeout)
        result.reachable = True
        cseq = 0

        async def request(method: str, auth: str | None = None) -> tuple[int, dict[str, list[str]], bytes]:
            nonlocal cseq
            cseq += 1
            lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: VMSMultimarca/1.0"]
            if method == "DESCRIBE":
                lines.append("Accept: application/sdp")
            if auth:
                lines.append(f"Authorization: {auth}")
            assert writer is not None
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
            await writer.drain()
            return await asyncio.wait_for(_read_response(reader), timeout)

        status, _, _ = await request("OPTIONS")
        status, headers, body = await request("DESCRIBE")
        if status == 401 and username:
            challenges = headers.get("www-authenticate", [])
            challenge = next((c for c in challenges if c.lower().startswith("digest")), None) or \
                (challenges[0] if challenges else "")
            auth = _auth_header(challenge, "DESCRIBE", url, username, password, 1) if challenge else None
            if auth:
                status, headers, body = await request("DESCRIBE", auth)
        result.status = status
        if status == 401:
            result.auth_ok = False
            result.error = "Usuario o contraseña RTSP incorrectos"
        elif status == 200:
            result.auth_ok = True
            sdp = body.decode("utf-8", errors="replace")
            result.codecs = [c.upper() for c in _RTPMAP_RE.findall(sdp)]
        elif status == 404:
            result.auth_ok = True if username else None
            result.error = "La ruta RTSP no existe en el equipo (404): revisa el canal o la ruta"
        else:
            result.error = f"El equipo respondió RTSP {status}"
    except (asyncio.TimeoutError, TimeoutError):
        result.error = "El puerto RTSP no responde (tiempo agotado)"
    except (ConnectionError, OSError) as exc:
        result.error = f"No se puede conectar al puerto RTSP {port}: {exc.strerror or type(exc).__name__}"
    except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        result.error = f"Respuesta RTSP no válida ({type(exc).__name__})"
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
    log.debug("Prueba RTSP %s → %s %s", url, result.status, result.error)
    return result


async def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port)), timeout)
    except (asyncio.TimeoutError, TimeoutError, OSError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
    return True


__all__ = ["RtspProbeResult", "probe_rtsp", "tcp_reachable"]
