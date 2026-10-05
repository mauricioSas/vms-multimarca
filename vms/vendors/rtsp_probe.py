"""Prueba RTSP sin ffmpeg: OPTIONS + DESCRIBE (y, si se pide, SETUP + PLAY hasta el primer fotograma clave).

Sirve para «Probar conexión» antes de dar de alta un equipo y para saber el códec de cada flujo leyendo el
SDP. Implementación propia de RFC 2326 con la autenticación de `vms.vendors.auth` (Digest SHA-256/MD5 de
RFC 7616 y Basic solo con `allow_basic`).

**Un solo intento con credenciales** (PLAN-V2 §3.2 punto 2): las credenciales se mandan una vez; si el
equipo vuelve a responder 401, no se reintenta (los equipos bloquean el usuario tras 3-5 fallos). Con la
contraseña ya aceptada sí se pueden pedir rutas alternativas del driver si la primera da 404.

Medidas para los avisos de GOP largo (PLAN-V2 §3.2 punto 7): lo que tarda el SDP y, con `first_frame=True`,
lo que tarda el primer fotograma clave por RTP entrelazado en TCP. Esa medida abre una sesión de vídeo
durante unos segundos y la cierra con TEARDOWN.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from vms.core import rtsp

from . import auth as vauth
from .errors import text_lock_hint
from .sdp import SdpInfo, parse_sdp

log = logging.getLogger("vms.vendors.rtsp")

MAX_HEADER = 64 * 1024
MAX_BODY = 256 * 1024
SLOW_SECONDS = 4.0          # más que esto (SDP o primer fotograma) = GOP largo: aviso y espera de 12 s en el muro
USER_AGENT = "VMSMultimarca/2.0"


@dataclass
class RtspProbeResult:
    reachable: bool = False
    status: int | None = None
    auth_ok: bool | None = None
    codecs: list[str] = field(default_factory=list)   # ["H264"], ["H265", "MPEG4-GENERIC"]...
    error: str = ""
    locked: bool = False
    lockout_minutes: int | None = None
    basic_only: bool = False            # el equipo solo ofreció Basic y no está permitido
    session_limit: bool = False         # 453/503: el equipo no admite más sesiones
    connection_refused: bool = False
    auth_scheme: str | None = None      # «digest-sha256», «digest-md5», «basic» o None si no hizo falta
    credentialed_requests: int = 0      # peticiones que llevaron credenciales (para las pruebas)
    path: str = ""                      # ruta que respondió (puede ser una alternativa del driver)
    sdp_ms: float | None = None
    first_frame_ms: float | None = None
    sdp: SdpInfo | None = None
    sdp_text: str = ""                  # SDP tal cual (sin credenciales; útil para capturar fixtures)

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def video_codec(self) -> str | None:
        return self.sdp.video_codec if self.sdp is not None else None

    @property
    def slow(self) -> bool:
        return any(v is not None and v > SLOW_SECONDS * 1000 for v in (self.sdp_ms, self.first_frame_ms))


class _Conn:
    """Una conexión RTSP con CSeq, sesión y lectura de mensajes y paquetes entrelazados."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float,
                 trace: list[dict[str, object]] | None = None) -> None:
        self.reader, self.writer, self.timeout = reader, writer, timeout
        self.cseq = 0
        self.session = ""
        self.trace = trace

    async def request(self, method: str, url: str, headers: dict[str, str] | None = None
                      ) -> tuple[int, str, dict[str, list[str]], bytes]:
        self.cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}", f"User-Agent: {USER_AGENT}"]
        if self.session and method not in ("OPTIONS", "DESCRIBE"):
            lines.append(f"Session: {self.session}")
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        await self.writer.drain()
        while True:
            item = await asyncio.wait_for(self._read_unit(), self.timeout)
            if isinstance(item, tuple):
                if self.trace is not None:   # para capturar fixtures: nunca la cabecera Authorization
                    status, reason, rh, rbody = item
                    self.trace.append({"method": method, "url": url, "authorized": "Authorization" in (headers or {}),
                                       "status": status, "reason": reason, "headers": rh,
                                       "body": rbody.decode("utf-8", errors="replace")})
                return item
            # paquete entrelazado que llega antes de la respuesta: se descarta

    async def _read_unit(self) -> tuple[int, str, dict[str, list[str]], bytes] | bytes:
        first = await self.reader.readexactly(1)
        if first == b"$":
            head = await self.reader.readexactly(3)
            size = int.from_bytes(head[1:3], "big")
            return bytes([head[0]]) + await self.reader.readexactly(size)
        head = first + await self.reader.readuntil(b"\r\n\r\n")
        if len(head) > MAX_HEADER:
            raise ValueError("cabecera RTSP demasiado grande")
        lines = head.decode("utf-8", errors="replace").split("\r\n")
        status_line = lines[0].split(" ", 2)
        if len(status_line) < 2 or not status_line[0].startswith("RTSP/"):
            raise ValueError("respuesta que no es RTSP")
        status = int(status_line[1])
        reason = status_line[2] if len(status_line) > 2 else ""
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers.setdefault(k.strip().lower(), []).append(v.strip())
        length = int((headers.get("content-length") or ["0"])[0] or 0)
        body = await self.reader.readexactly(min(length, MAX_BODY)) if length else b""
        return status, reason, headers, body

    async def read_packet(self) -> bytes | None:
        """Siguiente paquete entrelazado (canal + carga) o None si llega un mensaje RTSP."""
        item = await self._read_unit()
        return item if isinstance(item, bytes) else None


def is_keyframe(codec: str | None, payload: bytes) -> bool:
    """¿Este paquete RTP (carga sin la cabecera RTP) empieza o contiene un fotograma clave?"""
    if not payload:
        return False
    if codec in ("MJPEG", None, "MPEG-4", "MPEG-2", "H.263"):
        return True   # sin GOP que esperar (o códec que no sabemos mirar): el primer paquete vale
    if codec == "H.264":
        t = payload[0] & 0x1F
        if t == 5:
            return True
        if t == 24:   # STAP-A
            i = 1
            while i + 2 < len(payload):
                size = int.from_bytes(payload[i:i + 2], "big")
                if i + 2 < len(payload) and payload[i + 2] & 0x1F == 5:
                    return True
                i += 2 + size
            return False
        if t == 28 and len(payload) > 1:   # FU-A: bit de inicio + tipo
            return bool(payload[1] & 0x80) and payload[1] & 0x1F == 5
        return False
    if codec == "H.265":
        t = (payload[0] >> 1) & 0x3F
        if 16 <= t <= 21:
            return True
        if t == 48:   # AP
            i = 2
            while i + 2 < len(payload):
                size = int.from_bytes(payload[i:i + 2], "big")
                if i + 2 < len(payload) and 16 <= (payload[i + 2] >> 1) & 0x3F <= 21:
                    return True
                i += 2 + size
            return False
        if t == 49 and len(payload) > 2:   # FU
            return bool(payload[2] & 0x80) and 16 <= payload[2] & 0x3F <= 21
        return False
    return True


async def probe_rtsp(host: str, port: int, path: str, username: str = "", password: str = "", *,
                     timeout: float = 5.0, allow_basic: bool = False, alt_paths: tuple[str, ...] = (),
                     first_frame: bool = False, frame_timeout: float = 6.0,
                     trace: list[dict[str, object]] | None = None) -> RtspProbeResult:
    """OPTIONS + DESCRIBE a rtsp://host:port/path. Nunca lanza; devuelve el diagnóstico en español."""
    result = RtspProbeResult()
    base = f"rtsp://{rtsp.format_host(host)}:{int(port)}"
    writer: asyncio.StreamWriter | None = None
    candidates = [rtsp.normalize_path(path) or "/"] + [rtsp.normalize_path(p) for p in alt_paths if p]
    try:
        reader, w = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port)), timeout)
        writer = w
        result.reachable = True
        conn = _Conn(reader, w, timeout, trace)
        challenge: vauth.Challenge | None = None
        dstate: vauth.DigestState | None = None
        rejected = False
        await conn.request("OPTIONS", base + candidates[0])

        status, reason, body = 0, "", b""
        headers: dict[str, list[str]] = {}
        for cand in candidates:
            url = base + cand
            result.path = cand
            hdrs = {"Accept": "application/sdp"}
            if challenge is not None:
                hdrs["Authorization"] = vauth.authorization(challenge, "DESCRIBE", url, username, password,
                                                            state=dstate)
                result.credentialed_requests += 1
            t0 = time.monotonic()
            status, reason, headers, body = await conn.request("DESCRIBE", url, hdrs)
            if status == 401 and challenge is None:
                challenges = vauth.parse_challenges(headers.get("www-authenticate", []))
                if not username:
                    result.error = "El equipo pide usuario y contraseña para el vídeo RTSP"
                    result.auth_ok = False
                    break
                chosen = vauth.choose(challenges, allow_basic=allow_basic)
                if chosen is None:
                    if vauth.only_basic(challenges):
                        result.basic_only = True
                        result.error = ("El equipo solo admite autenticación Basic por RTSP (envía la contraseña "
                                        "sin cifrar). Actívala para este equipo si aceptas el riesgo")
                    else:
                        result.error = "El equipo pide una autenticación RTSP que no se reconoce"
                    break
                challenge, dstate = chosen, vauth.DigestState(chosen)
                result.auth_scheme = chosen.scheme
                hdrs["Authorization"] = vauth.authorization(challenge, "DESCRIBE", url, username, password,
                                                            state=dstate)
                result.credentialed_requests += 1
                t0 = time.monotonic()
                status, reason, headers, body = await conn.request("DESCRIBE", url, hdrs)
            if status in (401, 403) and challenge is not None:
                rejected = True
                break
            if status == 404 and cand != candidates[-1]:
                continue   # credenciales aceptadas (o no pedidas): se prueba la ruta alternativa
            result.sdp_ms = round((time.monotonic() - t0) * 1000, 1)
            break

        result.status = status or None
        text = body.decode("utf-8", errors="replace")
        if rejected or (status in (401, 403) and challenge is None and not result.error):
            locked, minutes = text_lock_hint(f"{reason}\n{text}")
            if locked or (status == 403 and challenge is not None and "lock" in reason.lower()):
                result.locked, result.lockout_minutes = True, minutes
                result.error = ("El equipo dice que el usuario está bloqueado por demasiados intentos" +
                                (f": espera {minutes} min" if minutes else ""))
            elif status == 403:
                result.error = "El usuario no tiene permiso para ver el vídeo RTSP (403)"
            else:
                result.error = "Usuario o contraseña RTSP incorrectos"
            result.auth_ok = False
        elif status == 200:
            result.auth_ok = True if challenge is not None else None
            result.sdp_text = text
            result.sdp = parse_sdp(text)
            result.codecs = [c.upper() for c in result.sdp.codecs]
            if first_frame:
                content_base = (headers.get("content-base") or headers.get("content-location") or [base + result.path])[0]
                await _first_frame(conn, result, content_base, challenge, dstate, username, password, frame_timeout)
        elif status == 404:
            result.auth_ok = True if challenge is not None else None
            result.error = "La ruta RTSP no existe en el equipo (404): revisa el canal o la ruta"
        elif status in (453, 503):
            result.auth_ok = True if challenge is not None else None
            result.session_limit = True
            result.error = ("El grabador no admite más sesiones de vídeo ahora mismo "
                            f"(RTSP {status}): conecta las cámaras directamente o baja la calidad")
        elif not result.error:
            result.error = f"El equipo respondió RTSP {status}"
    except (asyncio.TimeoutError, TimeoutError):
        result.error = "El puerto RTSP no responde (tiempo agotado)"
    except ConnectionRefusedError:
        result.connection_refused = True
        result.error = f"El equipo rechazó la conexión en el puerto RTSP {port}: RTSP puede estar desactivado"
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
    log.debug("Prueba RTSP %s%s → %s %s", base, result.path, result.status, result.error)
    return result


async def _first_frame(conn: _Conn, result: RtspProbeResult, content_base: str, challenge: vauth.Challenge | None,
                       dstate: vauth.DigestState | None, username: str, password: str, frame_timeout: float) -> None:
    """SETUP (TCP entrelazado) + PLAY de la pista de vídeo hasta el primer fotograma clave y TEARDOWN."""
    assert result.sdp is not None
    video = result.sdp.video
    if video is None:
        return

    def auth_hdr(method: str, url: str) -> dict[str, str]:
        if challenge is None:
            return {}
        return {"Authorization": vauth.authorization(challenge, method, url, username, password, state=dstate)}

    setup_url = result.sdp.control_url(video, content_base)
    t0 = time.monotonic()
    status, _, headers, _ = await conn.request(
        "SETUP", setup_url, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1", **auth_hdr("SETUP", setup_url)})
    if status in (453, 503):
        result.session_limit = True
        result.error = ("El grabador no admite más sesiones de vídeo ahora mismo "
                        f"(RTSP {status}): conecta las cámaras directamente o baja la calidad")
        return
    if status != 200:
        log.debug("SETUP respondió %s; no se mide el primer fotograma", status)
        return
    conn.session = (headers.get("session") or [""])[0].split(";")[0].strip()
    play_url = content_base
    status, _, _, _ = await conn.request("PLAY", play_url, {"Range": "npt=0.000-", **auth_hdr("PLAY", play_url)})
    if status in (453, 503):
        result.session_limit = True
        result.error = f"El grabador no admite más sesiones de vídeo ahora mismo (RTSP {status})"
        return
    if status != 200:
        return
    codec = video.normalized_codec
    deadline = t0 + frame_timeout
    try:
        while time.monotonic() < deadline:
            pkt = await asyncio.wait_for(conn.read_packet(), max(0.05, deadline - time.monotonic()))
            if not pkt or pkt[0] != 0 or len(pkt) < 13:
                continue
            rtp = pkt[1:]
            cc = rtp[0] & 0x0F
            offset = 12 + 4 * cc
            if rtp[0] & 0x10 and len(rtp) >= offset + 4:   # extensión de cabecera
                offset += 4 + 4 * int.from_bytes(rtp[offset + 2:offset + 4], "big")
            if is_keyframe(codec, rtp[offset:]):
                result.first_frame_ms = round((time.monotonic() - t0) * 1000, 1)
                break
    except (asyncio.TimeoutError, TimeoutError):
        pass
    if result.first_frame_ms is None:
        result.first_frame_ms = round(frame_timeout * 1000, 1)   # no llegó a tiempo: al menos esto
    try:
        conn.writer.write(f"TEARDOWN {play_url} RTSP/1.0\r\nCSeq: {conn.cseq + 1}\r\nSession: {conn.session}\r\n"
                          f"User-Agent: {USER_AGENT}\r\n\r\n".encode("utf-8"))
        await conn.writer.drain()
    except (ConnectionError, OSError):
        pass


async def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    return await tcp_state(host, port, timeout) == "open"


async def tcp_state(host: str, port: int, timeout: float = 3.0) -> str:
    """«open», «refused» (el equipo responde pero el puerto está cerrado) o «timeout»."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port)), timeout)
    except ConnectionRefusedError:
        return "refused"
    except (asyncio.TimeoutError, TimeoutError, OSError):
        return "timeout"
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
    return "open"


__all__ = ["RtspProbeResult", "SLOW_SECONDS", "is_keyframe", "probe_rtsp", "tcp_reachable", "tcp_state"]
