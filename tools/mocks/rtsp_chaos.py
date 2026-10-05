"""Servidor RTSP «caótico» propio (asyncio) para lo que MediaMTX no sabe simular (PLAN-V2 §3.3).

Escenarios por nombre, en el primer segmento de la ruta (`rtsp://…/<escenario>/stream`), en `?scenario=` o
como escenario por defecto del servidor. Se pueden combinar con «+» (`/digest-sha256+h265/stream`):

  Autenticación   digest-md5 (por defecto), digest-sha256, multi-challenge (Basic + MD5 + SHA-256 en tres
                  cabeceras), basic-only, no-auth (RTSP anónimo), lockout-after-3 (tras 3 fallos, 401 «Locked»
                  aunque la contraseña sea buena)
  SDP             sdp-no-control, sdp-absolute-control, sdp-no-rtpmap, sdp-no-sprop, sdp-lf-only
  Códec           h264 (por defecto), h265, mjpeg
  Rutas           double-slash (solo existe la ruta con «//»; el resto da 404)
  Sesiones        limit-2-sessions (453 a partir de la 3.ª sesión a la vez), busy-503 (DESCRIBE → 503)
  Conexión        drop-without-keepalive (corta a los 2 s de PLAY si no llega OPTIONS/GET_PARAMETER)
  Tiempo          slow-first-frame-10s (P-frames durante 10 s antes del primer IDR), slow-sdp (DESCRIBE
                  tarda 5 s)

El vídeo son NAL reales de 64x64 (H.264 baseline y H.265, `assets/chaos_streams.json`), así MediaMTX puede
leerlo como si fuera una cámara. Cuenta peticiones con credenciales, rechazos, esquemas usados y sesiones
simultáneas para que las pruebas comprueben el «un solo intento» y el límite de sesiones.

    async with RtspChaosServer(username="admin", password="Sim#Pass:1@/x") as srv:
        url = srv.url("digest-sha256")      # rtsp://127.0.0.1:<puerto>/digest-sha256/stream

Herramienta de laboratorio: nunca se distribuye.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from vms.vendors.auth import verify_digest

log = logging.getLogger("tools.mocks.rtsp_chaos")

ASSETS = json.loads((Path(__file__).parent / "assets" / "chaos_streams.json").read_text(encoding="utf-8"))
SCENARIOS = {
    "digest-md5", "digest-sha256", "multi-challenge", "basic-only", "no-auth", "lockout-after-3",
    "sdp-no-control", "sdp-absolute-control", "sdp-no-rtpmap", "sdp-no-sprop", "sdp-lf-only",
    "h264", "h265", "mjpeg", "double-slash", "limit-2-sessions", "busy-503", "drop-without-keepalive",
    "slow-first-frame-10s", "slow-sdp",
}
MAX_FRAGMENT = 1200


@dataclass
class ChaosStats:
    requests: list[str] = field(default_factory=list)
    credentialed: int = 0
    rejected: int = 0
    schemes: list[str] = field(default_factory=list)       # «digest-sha256», «digest-md5», «basic»
    active_sessions: int = 0
    max_sessions: int = 0
    refused_453: int = 0
    drops: int = 0
    connections: int = 0


def _b(x: str) -> bytes:
    return base64.b64decode(x)


def _fragments(codec: str, nal: bytes) -> list[bytes]:
    """NAL → cargas RTP (unidad única o FU-A/FU si no cabe)."""
    if len(nal) <= MAX_FRAGMENT:
        return [nal]
    out: list[bytes] = []
    if codec == "h264":
        indicator = (nal[0] & 0xE0) | 28
        ntype = nal[0] & 0x1F
        data = nal[1:]
        for i in range(0, len(data), MAX_FRAGMENT):
            chunk = data[i:i + MAX_FRAGMENT]
            start, end = i == 0, i + MAX_FRAGMENT >= len(data)
            out.append(bytes([indicator, (0x80 if start else 0) | (0x40 if end else 0) | ntype]) + chunk)
    else:
        ntype = (nal[0] >> 1) & 0x3F
        head = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
        data = nal[2:]
        for i in range(0, len(data), MAX_FRAGMENT):
            chunk = data[i:i + MAX_FRAGMENT]
            start, end = i == 0, i + MAX_FRAGMENT >= len(data)
            out.append(head + bytes([(0x80 if start else 0) | (0x40 if end else 0) | ntype]) + chunk)
    return out


class RtspChaosServer:
    def __init__(self, *, username: str = "admin", password: str = "Sim#Pass:1@/x", host: str = "127.0.0.1",
                 port: int = 0, scenario: str = "", realm: str = "IP Camera(CHAOS)", fps: int = 10,
                 slow_first_frame_s: float = 10.0, keepalive_timeout_s: float = 2.0, max_sessions: int = 2,
                 lock_after: int = 3) -> None:
        self.username, self.password, self.host, self.port = username, password, host, port
        self.scenario, self.realm, self.fps = scenario, realm, fps
        self.slow_first_frame_s, self.keepalive_timeout_s = slow_first_frame_s, keepalive_timeout_s
        self.max_sessions_allowed, self.lock_after = max_sessions, lock_after
        self.stats = ChaosStats()
        self.locked = False
        self._nonces: set[str] = set()
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ ciclo de vida
    async def start(self) -> RtspChaosServer:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> RtspChaosServer:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def url(self, scenario: str = "", path: str = "stream", *, credentials: bool = False) -> str:
        from urllib.parse import quote
        auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@" if credentials else ""
        prefix = f"/{scenario}" if scenario else ""
        return f"rtsp://{auth}{self.host}:{self.port}{prefix}/{path}"

    # ------------------------------------------------------------------ escenarios
    def scenarios_of(self, url: str) -> set[str]:
        parts = urlsplit(url)
        names: set[str] = set(filter(None, self.scenario.split("+")))
        first = parts.path.lstrip("/").split("/", 1)[0]
        names |= {n for n in first.split("+") if n in SCENARIOS}
        for v in parse_qs(parts.query).get("scenario", []):
            names |= {n for n in v.split("+") if n in SCENARIOS}
        return names

    def codec_of(self, sc: set[str]) -> str:
        return "h265" if "h265" in sc else "mjpeg" if "mjpeg" in sc else "h264"

    def _challenges(self, sc: set[str]) -> list[str]:
        def digest(alg: str) -> str:
            nonce = secrets.token_hex(16)
            self._nonces.add(nonce)
            return f'Digest realm="{self.realm}", nonce="{nonce}", algorithm={alg}, qop="auth"'
        if "basic-only" in sc:
            return [f'Basic realm="{self.realm}"']
        if "multi-challenge" in sc:
            return [f'Basic realm="{self.realm}"', digest("MD5"), digest("SHA-256")]
        if "digest-sha256" in sc:
            return [digest("SHA-256")]
        return [digest("MD5")]

    def _check_auth(self, method: str, header: str, sc: set[str]) -> bool:
        if "no-auth" in sc:
            return True
        if not header:
            return False
        self.stats.credentialed += 1
        scheme, _, value = header.partition(" ")
        ok = False
        if scheme.lower() == "basic" and ("basic-only" in sc or "multi-challenge" in sc):
            try:
                user, _, pw = base64.b64decode(value).decode("utf-8").partition(":")
                ok = user == self.username and pw == self.password
            except (ValueError, UnicodeDecodeError):
                ok = False
            if ok:
                self.stats.schemes.append("basic")
        elif scheme.lower() == "digest" and "basic-only" not in sc:
            sha = "algorithm=SHA-256" in header
            if "digest-sha256" in sc and not sha:
                ok = False
            else:
                ok = verify_digest(header, method, self.username, self.password, self.realm, self._nonces)
            if ok:
                self.stats.schemes.append("digest-sha256" if sha else "digest-md5")
        if "lockout-after-3" in sc and self.locked:
            ok = False
        if not ok:
            self.stats.rejected += 1
            if "lockout-after-3" in sc and self.stats.rejected >= self.lock_after:
                self.locked = True
        return ok

    def sdp(self, sc: set[str], base_url: str) -> str:
        codec = self.codec_of(sc)
        lines = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=Chaos", "c=IN IP4 0.0.0.0", "t=0 0"]
        if "sdp-no-control" not in sc:
            lines.append("a=control:*")
        if codec == "mjpeg":
            lines += ["m=video 0 RTP/AVP 26"]
        else:
            lines.append("m=video 0 RTP/AVP 96")
            if "sdp-no-rtpmap" not in sc:
                lines.append(f"a=rtpmap:96 {'H264' if codec == 'h264' else 'H265'}/90000")
            params = ASSETS[codec]["params"]
            if codec == "h264":
                fmtp = "a=fmtp:96 packetization-mode=1;profile-level-id=42c00a"
                if "sdp-no-sprop" not in sc:
                    fmtp += ";sprop-parameter-sets=" + ",".join(params)
            else:
                fmtp = "a=fmtp:96 "
                if "sdp-no-sprop" not in sc:
                    fmtp += f"sprop-vps={params[0]};sprop-sps={params[1]};sprop-pps={params[2]}"
                else:
                    fmtp += "profile-id=1"
            lines.append(fmtp)
        if "sdp-no-control" not in sc:
            lines.append(f"a=control:{base_url.rstrip('/')}/trackID=1" if "sdp-absolute-control" in sc
                         else "a=control:trackID=1")
        sep = "\n" if "sdp-lf-only" in sc else "\r\n"
        return sep.join(lines) + sep

    # ------------------------------------------------------------------ conexión
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self.stats.connections += 1
        session = ""
        playing: asyncio.Task[None] | None = None
        activity = [time.monotonic()]
        counted_session = False
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                    return
                if head.startswith(b"$"):
                    continue
                lines = head.decode("utf-8", errors="replace").split("\r\n")
                method, url = (lines[0].split(" ") + ["", ""])[:2]
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                length = int(headers.get("content-length", "0") or 0)
                if length:
                    await reader.readexactly(length)
                cseq = headers.get("cseq", "0")
                self.stats.requests.append(f"{method} {urlsplit(url).path}")
                activity[0] = time.monotonic()
                sc = self.scenarios_of(url)
                extra: dict[str, str] = {}
                body = b""

                if method in ("OPTIONS", "GET_PARAMETER"):
                    status, reason = 200, "OK"
                    extra["Public"] = "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"
                elif method == "DESCRIBE":
                    if "busy-503" in sc:
                        status, reason = 503, "Service Unavailable"
                    elif not self._check_auth(method, headers.get("authorization", ""), sc):
                        status, reason = 401, "Unauthorized"
                        if "lockout-after-3" in sc and self.locked:
                            reason = "Unauthorized (User Locked)"
                            body = b"User locked: too many failed attempts, try again in 30 minutes"
                        writer.write(self._response(status, reason, cseq, {"Content-Type": "text/plain"} if body else {},
                                                    body, self._challenges(sc)))
                        await writer.drain()
                        continue
                    elif "double-slash" in sc and "//" not in urlsplit(url).path.split("double-slash", 1)[-1]:
                        status, reason = 404, "Not Found"
                    else:
                        if "slow-sdp" in sc:
                            await asyncio.sleep(5.0)
                        status, reason = 200, "OK"
                        body = self.sdp(sc, url).encode("utf-8")
                        extra["Content-Type"] = "application/sdp"
                        extra["Content-Base"] = url.rstrip("/") + "/"
                elif method == "SETUP":
                    if "no-auth" not in sc and not self._check_auth(method, headers.get("authorization", ""), sc):
                        status, reason = 401, "Unauthorized"
                    elif "limit-2-sessions" in sc and self.stats.active_sessions >= self.max_sessions_allowed \
                            and not counted_session:
                        status, reason = 453, "Not Enough Bandwidth"
                        self.stats.refused_453 += 1
                    else:
                        status, reason = 200, "OK"
                        session = session or secrets.token_hex(8)
                        timeout = int(self.keepalive_timeout_s) if "drop-without-keepalive" in sc else 60
                        extra["Session"] = f"{session};timeout={timeout}"
                        extra["Transport"] = "RTP/AVP/TCP;unicast;interleaved=0-1"
                        if not counted_session:
                            counted_session = True
                            self.stats.active_sessions += 1
                            self.stats.max_sessions = max(self.stats.max_sessions, self.stats.active_sessions)
                elif method == "PLAY":
                    status, reason = (200, "OK") if session else (454, "Session Not Found")
                    if session:
                        extra["Session"] = session
                elif method == "TEARDOWN":
                    status, reason = 200, "OK"
                    writer.write(self._response(status, reason, cseq, {}, b""))
                    await writer.drain()
                    return
                else:
                    status, reason = 405, "Method Not Allowed"
                writer.write(self._response(status, reason, cseq, extra, body,
                                            self._challenges(sc) if status == 401 else None))
                await writer.drain()
                if method == "PLAY" and status == 200 and playing is None:
                    playing = asyncio.create_task(self._stream(writer, sc))
                    if "drop-without-keepalive" in sc:
                        asyncio.create_task(self._watchdog(writer, activity))
        except (ConnectionError, OSError):
            return
        finally:
            if playing is not None:
                playing.cancel()
            if counted_session:
                self.stats.active_sessions -= 1
            try:
                writer.close()
            except (ConnectionError, OSError, RuntimeError):
                pass
            if task is not None:
                self._tasks.discard(task)

    async def _watchdog(self, writer: asyncio.StreamWriter, activity: list[float]) -> None:
        while not writer.is_closing():
            await asyncio.sleep(0.2)
            if time.monotonic() - activity[0] > self.keepalive_timeout_s * 1.5:
                self.stats.drops += 1
                writer.close()
                return

    @staticmethod
    def _response(status: int, reason: str, cseq: str, headers: dict[str, str], body: bytes,
                  challenges: list[str] | None = None) -> bytes:
        lines = [f"RTSP/1.0 {status} {reason}", f"CSeq: {cseq}", "Server: Chaos/1.0"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        lines += [f"WWW-Authenticate: {c}" for c in challenges or []]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

    async def _stream(self, writer: asyncio.StreamWriter, sc: set[str]) -> None:
        codec = self.codec_of(sc)
        seq, ts, ssrc = 1, 0, 0x1234ABCD
        pt = 26 if codec == "mjpeg" else 96

        def packet(payload: bytes, marker: bool) -> bytes:
            nonlocal seq
            hdr = bytes([0x80, (0x80 if marker else 0) | pt]) + seq.to_bytes(2, "big") + ts.to_bytes(4, "big") \
                + ssrc.to_bytes(4, "big")
            seq = (seq + 1) & 0xFFFF
            rtp = hdr + payload
            return b"$\x00" + len(rtp).to_bytes(2, "big") + rtp

        if codec == "mjpeg":
            jpeg_hdr = bytes([0, 0, 0, 0, 1, 255, 8, 8])   # cabecera RTP/JPEG mínima (no decodificable)
            while not writer.is_closing():
                writer.write(packet(jpeg_hdr + b"\x00" * 64, True))
                await writer.drain()
                ts = (ts + 90000 // self.fps) & 0xFFFFFFFF
                await asyncio.sleep(1 / self.fps)
            return
        params = [_b(p) for p in ASSETS[codec]["params"]]
        frames = [[_b(n) for n in f] for f in ASSETS[codec]["frames"]]
        start = time.monotonic()
        delay_idr = self.slow_first_frame_s if "slow-first-frame-10s" in sc else 0.0
        i = 1 if delay_idr else 0
        while not writer.is_closing():
            if delay_idr and time.monotonic() - start >= delay_idr:
                delay_idr, i = 0.0, 0
            frame = frames[i % len(frames)] if not delay_idr else frames[1 + (i % (len(frames) - 1))]
            nals = (params + frame) if (i % len(frames) == 0 and not delay_idr) else frame
            payloads: list[bytes] = []
            for n in nals:
                payloads += _fragments(codec, n)
            for k, p in enumerate(payloads):
                writer.write(packet(p, k == len(payloads) - 1))
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                return
            ts = (ts + 90000 // self.fps) & 0xFFFFFFFF
            i += 1
            await asyncio.sleep(1 / self.fps)


async def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Servidor RTSP caótico de laboratorio")
    ap.add_argument("--port", type=int, default=8555)
    ap.add_argument("--scenario", default="")
    args = ap.parse_args()
    srv = await RtspChaosServer(port=args.port, scenario=args.scenario).start()
    print(f"rtsp://admin:***@127.0.0.1:{srv.port}/<escenario>/stream  (Ctrl+C para salir)")
    try:
        await asyncio.Event().wait()
    finally:
        await srv.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
