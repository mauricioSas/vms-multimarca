"""FakeEngine nunca apunta al motor del programa instalado.

Fallo real (8/10/2026): mientras pasaban las pruebas en el PC con el programa instalado, su MediaMTX recibía
peticiones RTSP y WebRTC de cámaras inventadas («path 'cam-xxxxxxxx/sub' is not configured»). FakeEngine daba
http://127.0.0.1:8889/…/whep, rtsp://127.0.0.1:8554/… y http://127.0.0.1:9996/get…: el proxy WHEP, la
reproducción y la instantánea por OpenCV de las pruebas llegaban al MediaMTX de verdad.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from tests.fakes import FakeEngine
from vms.core.settings import VmsSettings

CAM = "cam-0000abcd"


def engine_default_ports() -> set[int]:
    """Los puertos por defecto del motor (los del MediaMTX instalado), leídos de VmsSettings."""
    fields = VmsSettings.model_fields
    return {int(str(fields[name].default).rpartition(":")[2]) for name in (
        "mtx_rtsp_address", "mtx_webrtc_address", "mtx_webrtc_ice_udp", "mtx_api_address", "mtx_playback_address")}


def urls(engine: FakeEngine) -> list[str]:
    return [engine.whep_url(CAM, "sub"), engine.rtsp_read_url(CAM, "main"),
            engine.playback_get_url(CAM, datetime(2026, 10, 8, 10, tzinfo=timezone.utc), 60.0)]


def test_fake_engine_urls_never_point_at_the_installed_engine() -> None:
    assert engine_default_ports() == {8554, 8889, 8189, 9997, 9996}
    for url in urls(FakeEngine()):
        parts = urlsplit(url)
        assert parts.hostname == "127.0.0.1" and parts.port is not None, url
        assert parts.port not in engine_default_ports(), url
        assert CAM in url


def test_the_ports_are_the_engine_attributes() -> None:
    """Las pruebas que montan su propio servidor de WHEP o de reproducción saben a qué puerto va cada cosa."""
    e = FakeEngine()
    assert [urlsplit(u).port for u in urls(e)] == [e.webrtc_port, e.rtsp_port, e.playback_port]
