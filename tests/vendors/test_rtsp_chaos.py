"""Prueba RTSP (`probe_rtsp`) contra el servidor caótico: autenticación, SDP raros, sesiones, bloqueo y GOP."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from tools.mocks.rtsp_chaos import RtspChaosServer
from vms.vendors.rtsp_probe import probe_rtsp

PW = "Sim#Pass:1@/x"


@pytest.fixture
async def chaos() -> AsyncIterator[RtspChaosServer]:
    async with RtspChaosServer(password=PW, slow_first_frame_s=5.0) as srv:
        yield srv


@pytest.mark.parametrize(("scenario", "scheme", "codec"), [
    ("digest-md5", "digest-md5", "H.264"),
    ("digest-sha256", "digest-sha256", "H.264"),
    ("multi-challenge", "digest-sha256", "H.264"),   # elige el más fuerte de 3 retos
    ("h265", "digest-md5", "H.265"),
    ("mjpeg", "digest-md5", "MJPEG"),
    ("sdp-no-control", "digest-md5", "H.264"),
    ("sdp-absolute-control", "digest-md5", "H.264"),
    ("sdp-no-rtpmap", "digest-md5", "H.264"),
    ("sdp-no-sprop", "digest-md5", "H.264"),
    ("sdp-lf-only", "digest-md5", "H.264"),
    ("h265+sdp-no-rtpmap", "digest-md5", "H.265"),
])
async def test_auth_and_sdp_scenarios(chaos: RtspChaosServer, scenario: str, scheme: str, codec: str) -> None:
    r = await probe_rtsp("127.0.0.1", chaos.port, f"/{scenario}/stream", "admin", PW, first_frame=True,
                         frame_timeout=3)
    assert r.ok, r.error
    assert r.auth_scheme == scheme and r.video_codec == codec
    assert r.credentialed_requests == 1 and r.auth_ok is True
    assert r.first_frame_ms is not None and r.first_frame_ms < 3000 and not r.slow


async def test_basic_only_needs_allow_basic(chaos: RtspChaosServer) -> None:
    r = await probe_rtsp("127.0.0.1", chaos.port, "/basic-only/stream", "admin", PW)
    assert not r.ok and r.basic_only and r.credentialed_requests == 0 and chaos.stats.credentialed == 0
    assert "Basic" in r.error
    r = await probe_rtsp("127.0.0.1", chaos.port, "/basic-only/stream", "admin", PW, allow_basic=True)
    assert r.ok and r.auth_scheme == "basic"


async def test_wrong_password_is_one_attempt(chaos: RtspChaosServer, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    r = await probe_rtsp("127.0.0.1", chaos.port, "/digest-sha256/stream", "admin", "Mala#1")
    assert r.status == 401 and r.auth_ok is False and not r.locked
    assert r.credentialed_requests == 1 and chaos.stats.credentialed == 1 and chaos.stats.rejected == 1
    assert "Mala#1" not in caplog.text and PW not in caplog.text


async def test_lockout_is_distinguished_from_bad_password(chaos: RtspChaosServer) -> None:
    results = [await probe_rtsp("127.0.0.1", chaos.port, "/lockout-after-3/stream", "admin", "Mala#1")
               for _ in range(3)]
    assert [r.locked for r in results] == [False, False, True]
    assert results[2].lockout_minutes == 30 and "bloqueado" in results[2].error
    good = await probe_rtsp("127.0.0.1", chaos.port, "/lockout-after-3/stream", "admin", PW)
    assert good.locked and not good.ok          # con la buena también: el equipo sigue bloqueado


async def test_busy_and_session_limit(chaos: RtspChaosServer) -> None:
    busy = await probe_rtsp("127.0.0.1", chaos.port, "/busy-503/stream", "admin", PW)
    assert busy.session_limit and busy.status == 503 and "sesiones" in busy.error

    async def hold() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # cliente que abre una sesión y la mantiene (como un muro)
        reader, writer = await asyncio.open_connection("127.0.0.1", chaos.port)
        url = chaos.url("limit-2-sessions+no-auth")
        for i, (method, extra) in enumerate([("DESCRIBE", ""), ("SETUP", "Transport: RTP/AVP/TCP;interleaved=0-1\r\n")]):
            writer.write(f"{method} {url}/trackID=1 RTSP/1.0\r\nCSeq: {i + 1}\r\n{extra}\r\n".encode())
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            length = next((int(x.split(b":")[1]) for x in head.split(b"\r\n") if x.lower().startswith(b"content-length")), 0)
            if length:
                await reader.readexactly(length)
        return reader, writer

    held = [await hold(), await hold()]
    assert chaos.stats.active_sessions == 2
    r = await probe_rtsp("127.0.0.1", chaos.port, "/limit-2-sessions+no-auth/stream", "admin", PW,
                         first_frame=True, frame_timeout=2)
    assert r.ok and r.session_limit and "sesiones" in r.error and chaos.stats.refused_453 == 1
    for _, w in held:
        w.close()


async def test_double_slash_variant_only_after_404(chaos: RtspChaosServer) -> None:
    async with RtspChaosServer(password=PW, scenario="double-slash") as srv:
        r = await probe_rtsp("127.0.0.1", srv.port, "/main", "admin", PW, alt_paths=("//main",))
        assert r.ok and r.path == "//main"
        # la alternativa se pidió con las credenciales ya aceptadas: ningún rechazo
        assert srv.stats.rejected == 0
        bad = await probe_rtsp("127.0.0.1", srv.port, "/main", "admin", "Mala#1", alt_paths=("//main",))
        assert bad.auth_ok is False and bad.credentialed_requests == 1   # tras un 401 no se prueba nada más


async def test_slow_first_frame_is_measured(chaos: RtspChaosServer) -> None:
    r = await probe_rtsp("127.0.0.1", chaos.port, "/slow-first-frame-10s/stream", "admin", PW,
                         first_frame=True, frame_timeout=8)
    assert r.ok and r.first_frame_ms is not None and r.first_frame_ms >= 4500 and r.slow


async def test_drop_without_keepalive() -> None:
    async with RtspChaosServer(password=PW, keepalive_timeout_s=0.5) as srv:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        url = srv.url("drop-without-keepalive+no-auth")
        for i, (method, extra) in enumerate([("SETUP", "Transport: RTP/AVP/TCP;interleaved=0-1\r\n"),
                                             ("PLAY", "")]):
            sess = "" if method == "SETUP" else "Session: x\r\n"
            writer.write(f"{method} {url} RTSP/1.0\r\nCSeq: {i + 1}\r\n{extra}{sess}\r\n".encode())
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
        t0 = asyncio.get_running_loop().time()
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            while asyncio.get_running_loop().time() - t0 < 5:
                await reader.readexactly(4)
                size = 0
                await reader.readexactly(size)
        assert srv.stats.drops == 1
        writer.close()


async def test_unreachable_and_refused() -> None:
    srv = await RtspChaosServer().start()
    port = srv.port
    await srv.stop()
    r = await probe_rtsp("127.0.0.1", port, "/x", "admin", PW, timeout=1)
    assert not r.reachable and r.connection_refused and "rechazó" in r.error
