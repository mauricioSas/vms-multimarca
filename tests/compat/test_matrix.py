"""Matriz de integración con simuladores (PLAN-V2 §4.2), parte de drivers (B5).

MediaMTX de laboratorio + ffmpeg hacen de cámaras «de verdad» (códecs, audio, GOP, Digest/Basic); los mocks de
Hikvision/Dahua, el servidor caótico y los respondedores de descubrimiento hacen el resto. Los casos de la matriz
que dependen del motor o del muro (grabar, WebRTC, reconexión) los cubren las pruebas de B1/B2 en la
integración; aquí se prueba lo que hace el driver con cada caso. Estado de cada caso: `tests/compat/LEEME.md`.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.compat.conftest import H264_BASE, LAB_PASSWORD, LabMtx, Publisher
from tests.vendors.conftest import TEST_PASSWORD, asgi, make_device
from tools.mocks.hikvision import HikChannel, HikvisionMock, nvr_channels
from tools.mocks.rtsp_chaos import RtspChaosServer
from vms.core.credentials import CredentialStore
from vms.core.models import AppConfig, Camera
from vms.core.sources import build_camera_sources
from vms.vendors import client_for, probe_rtsp, test_device

pytestmark = [pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg, pytest.mark.slow]


@pytest.mark.parametrize(("path", "codec", "audio"), [
    ("Streaming/Channels/101", "H.264", None),
    ("stream1", "H.264", None),                     # high con B-frames
    ("unicast/c1/s0/live", "H.265", "MPEG4-GENERIC"),
    ("unicast/c1/s1/live", "H.264", "PCMA"),
    ("Preview_01_main", "MJPEG", None),
])
async def test_codecs_and_audio_through_mediamtx(lab: LabMtx, path: str, codec: str, audio: str | None) -> None:
    r = await probe_rtsp("127.0.0.1", lab.rtsp_port, f"/{path}", "admin", LAB_PASSWORD, first_frame=True,
                         frame_timeout=6)
    assert r.ok and r.video_codec == codec and r.auth_scheme == "digest-md5", r.error
    if audio:
        assert audio in r.codecs
    assert r.first_frame_ms is not None and r.first_frame_ms < 6000


async def test_h265_main_h264_sub_profile(lab: LabMtx) -> None:
    """Perfil Uniview contra cámara con principal H.265 + AAC: alta correcta y aviso de H.265."""
    dev = make_device("uniview", rtsp_port=lab.rtsp_port, onvif_port=1)
    r = await test_device(dev, LAB_PASSWORD)
    assert r.ok and r.rtsp_ok and any("H.265" in w for w in r.warnings)


async def test_mjpeg_is_flagged_for_the_wall(lab: LabMtx) -> None:
    r = await test_device(make_device("reolink", rtsp_port=lab.rtsp_port), LAB_PASSWORD)
    assert r.ok and any("MJPEG" in w for w in r.warnings)


async def test_h265_sub_offers_codec_fix_and_mock_accepts_put(lab: LabMtx) -> None:
    mock = HikvisionMock(kind="camera", password=LAB_PASSWORD, channels=[HikChannel("Cam", sub_codec="H.265")])
    dev = make_device("hikvision", rtsp_port=lab.rtsp_port)
    r = await test_device(dev, LAB_PASSWORD, transport=asgi(mock.app))
    assert r.ok and any("Corregir códec" in w for w in r.warnings)
    sub = await probe_rtsp("127.0.0.1", lab.rtsp_port, "/Streaming/Channels/102", "admin", LAB_PASSWORD)
    assert sub.video_codec == "H.265"                       # lo que de verdad llega por el subflujo
    client = client_for(dev, LAB_PASSWORD, transport=asgi(mock.app))
    try:
        await client.set_stream_codec(1, "sub", "H.264")   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert mock.channels[0].sub_codec == "H.264" and len(mock.puts) == 1


async def test_long_gop_first_frame_under_12s(lab: LabMtx) -> None:
    r = await probe_rtsp("127.0.0.1", lab.rtsp_port, "/gop10", "admin", LAB_PASSWORD, first_frame=True,
                         frame_timeout=12)
    assert r.ok and r.first_frame_ms is not None and r.first_frame_ms < 12000
    assert r.slow == (r.first_frame_ms > 4000)


async def test_basic_only_camera_needs_allow_basic(tmp_path: Any, mediamtx_bin: str, ffmpeg_bin: str) -> None:
    mtx = LabMtx(mediamtx_bin, tmp_path, auth="basic").start()
    pub = Publisher(ffmpeg_bin, mtx.url("stream1"), H264_BASE, log=tmp_path / "ff.log").start()
    try:
        await asyncio.to_thread(mtx.wait_ready, {"stream1"})
        denied = await test_device(make_device("tplink-vigi", rtsp_port=mtx.rtsp_port, onvif_port=1), LAB_PASSWORD)
        assert not denied.ok and any("Basic" in w for w in denied.warnings)
        allowed = await test_device(make_device("tplink-vigi", rtsp_port=mtx.rtsp_port, onvif_port=1,
                                                allow_basic=True), LAB_PASSWORD)
        assert allowed.ok and any("sin cifrar" in w for w in allowed.warnings)
    finally:
        pub.stop()
        mtx.stop()


@pytest.mark.parametrize("scenario", ["digest-md5", "digest-sha256"])
async def test_one_upstream_session_for_many_readers(tmp_path: Any, mediamtx_bin: str, scenario: str) -> None:
    """NVR que solo admite 2 sesiones: MediaMTX abre UNA hacia el equipo aunque haya 4 «muros» leyendo.
    Con digest-sha256 se comprueba además que MediaMTX (gortsplib) responde a un reto SHA-256."""
    async with RtspChaosServer(password=TEST_PASSWORD) as chaos:
        src = chaos.url(f"{scenario}+limit-2-sessions", credentials=True)
        mtx = LabMtx(mediamtx_bin, tmp_path, paths={"nvrch1": {"source": src, "sourceOnDemand": "no",
                                                               "rtspTransport": "tcp"}}).start()
        try:
            await asyncio.to_thread(mtx.wait_ready, {"nvrch1"}, 20)
            readers = await asyncio.gather(*[
                probe_rtsp("127.0.0.1", mtx.rtsp_port, "/nvrch1", "admin", LAB_PASSWORD, first_frame=True,
                           frame_timeout=5) for _ in range(4)])
            assert all(r.ok and r.first_frame_ms is not None and r.first_frame_ms < 5000 for r in readers)
            assert chaos.stats.max_sessions == 1 and chaos.stats.refused_453 == 0
            assert chaos.stats.schemes and set(chaos.stats.schemes) == {scenario}
        finally:
            mtx.stop()


async def test_nvr_16_channels_import_and_sources(credential_store: CredentialStore) -> None:
    """NVR de 16 canales con 2 sin vídeo: canales, estados y 16 rutas con credenciales codificadas."""
    mock = HikvisionMock(password=TEST_PASSWORD, channels=nvr_channels(16, offline=(5, 12), h265=(3,)))
    dev = make_device("hikvision", kind="nvr")
    client = client_for(dev, TEST_PASSWORD, transport=asgi(mock.app))
    try:
        chans = await client.list_channels()
    finally:
        await client.aclose()
    assert [c.channel for c in chans] == list(range(1, 17))
    assert [c.channel for c in chans if c.online is False] == [5, 12]
    cfg = AppConfig(devices=[dev], cameras=[Camera(name=f"C{c.channel}", device_id=dev.id, channel=c.channel)
                                            for c in chans])
    credential_store.set_device_password(dev.id, TEST_PASSWORD)
    sources = build_camera_sources(cfg, credential_store)
    assert len(sources) == 16
    assert sources[15].main_url.endswith("/Streaming/Channels/1601") and "Sim%23Pass%3A1%40%2Fx" in sources[0].main_url


async def test_hybrid_dvr_ids_map_to_rtsp_paths(credential_store: CredentialStore) -> None:
    mock = HikvisionMock(kind="dvr", password=TEST_PASSWORD, channels=nvr_channels(4))
    dev = make_device("hikvision", kind="dvr")
    client = client_for(dev, TEST_PASSWORD, transport=asgi(mock.app))
    try:
        chans = await client.list_channels()
    finally:
        await client.aclose()
    ip = [c.channel for c in chans if c.analog is False]
    assert ip == [33, 34, 35, 36]
    cfg = AppConfig(devices=[dev], cameras=[Camera(name=f"C{n}", device_id=dev.id, channel=n) for n in ip])
    credential_store.set_device_password(dev.id, TEST_PASSWORD)
    urls = [s.main_url.rsplit("/", 1)[-1] for s in build_camera_sources(cfg, credential_store)]
    assert urls == ["3301", "3401", "3501", "3601"]
