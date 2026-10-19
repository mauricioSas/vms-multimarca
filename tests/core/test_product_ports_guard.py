"""El vigilante de los puertos del programa instalado (tests/conftest.py, ProductPortGuard).

Regresión del 8/10/2026: mientras pasaban las pruebas en el PC con el programa instalado, su MediaMTX
(127.0.0.1:8554 RTSP y 127.0.0.1:8889 WebRTC) recibía peticiones de cámaras inventadas por las pruebas
(«path 'cam-xxxxxxxx/sub' is not configured»). Estas pruebas nunca tocan los puertos de verdad: le dan al
vigilante un puerto libre como si fuera del programa.
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.conftest import (PRODUCT_PORT_GUARD, ProductPortGuard, ProductPortTouched, get_free_port,
                            isolated_settings, product_default_ports)
from tests.fakes import FakeEngine
from vms.core.settings import VmsSettings

ROLE = "un puerto de prueba"
CAM = "cam-0000abcd"


@pytest.fixture
def fake_product_port(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Un puerto libre (nadie escucha) que el vigilante trata como del programa. Las anotaciones se borran al
    final: si no, el propio vigilante haría fallar la prueba."""
    port = get_free_port()
    monkeypatch.setattr(PRODUCT_PORT_GUARD, "ports", {port: ROLE})
    try:
        yield port
    finally:
        PRODUCT_PORT_GUARD.hits.clear()


def _udp_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_default_ports_are_the_installed_product_ones() -> None:
    ports = product_default_ports()
    assert set(ports) == {8600, 8643, 8554, 8889, 8189, 9996, 9997}
    assert ports[8554] == "el vídeo RTSP del motor" and ports[8889] == "la vista en vivo (WebRTC) del motor"
    assert ports[9996] == "la reproducción de grabaciones del motor" and ports[8600] == "la web del programa"
    assert PRODUCT_PORT_GUARD.ports == ports


def test_guard_is_on_during_tests_and_knows_local_addresses() -> None:
    assert PRODUCT_PORT_GUARD.active
    for host in ("127.0.0.1", "127.0.1.1", "::1", "[::1]", "localhost", "0.0.0.0", "::", "::ffff:127.0.0.1"):
        assert ProductPortGuard.is_local(host), host
    for host in ("192.168.1.64", "10.0.0.5", "camara.local", "example.com", ""):
        assert not ProductPortGuard.is_local(host), host


@pytest.mark.product_ports
def test_marker_lets_a_test_talk_to_the_product() -> None:
    assert not PRODUCT_PORT_GUARD.active


def _ipv6_loopback() -> bool:
    """¿Hay ::1 de verdad? (en un contenedor sin IPv6 Python puede tenerlo compilado y el sistema no)."""
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_socket_connect_is_refused_at_once_and_reported(host: str, fake_product_port: int) -> None:
    if host != "127.0.0.1" and not _ipv6_loopback():
        pytest.skip("sin IPv6 en este equipo («localhost» también puede ser ::1)")
    t0 = time.monotonic()
    with pytest.raises(ProductPortTouched) as exc:
        socket.create_connection((host, fake_product_port), timeout=5)
    # Sin esperar los 3 s que Windows tarda en dar por cerrado un puerto local: ni siquiera se intenta
    assert time.monotonic() - t0 < 1.5
    assert isinstance(exc.value, ConnectionRefusedError) and ROLE in str(exc.value)
    # «localhost» son dos intentos (::1 y 127.0.0.1): los dos cortados
    assert PRODUCT_PORT_GUARD.hits and all(h.endswith(f":{fake_product_port} ({ROLE}) por socket.connect")
                                           for h in PRODUCT_PORT_GUARD.hits), PRODUCT_PORT_GUARD.hits


def test_connect_ex_and_udp_sendto_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    tcp, udp = get_free_port(), _udp_free_port()
    monkeypatch.setattr(PRODUCT_PORT_GUARD, "ports", {tcp: ROLE, udp: ROLE})
    try:
        with socket.socket() as s, pytest.raises(ProductPortTouched):
            s.connect_ex(("127.0.0.1", tcp))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(ProductPortTouched):
            s.sendto(b"x", ("127.0.0.1", udp))
        assert [h.rsplit(" por ", 1)[1] for h in PRODUCT_PORT_GUARD.hits] == ["socket.connect", "socket.sendto"]
    finally:
        PRODUCT_PORT_GUARD.hits.clear()


def test_other_ports_and_other_hosts_are_untouched(fake_product_port: int) -> None:
    other = get_free_port()
    with pytest.raises(ConnectionRefusedError) as exc:
        socket.create_connection(("127.0.0.1", other), timeout=5)
    assert not isinstance(exc.value, ProductPortTouched)
    # El mismo número de puerto en otra máquina (una cámara de la LAN) no es el programa instalado
    assert PRODUCT_PORT_GUARD.verdict("192.168.1.64", fake_product_port, "prueba") == ""
    assert PRODUCT_PORT_GUARD.hits == []


def test_httpx_sync_gets_a_connect_error_and_the_test_is_reported(fake_product_port: int) -> None:
    with pytest.raises(httpx.ConnectError):
        httpx.post(f"http://127.0.0.1:{fake_product_port}/cam-0000abcd/sub/whep", content=b"v=0", timeout=5)
    assert len(PRODUCT_PORT_GUARD.hits) == 1


@pytest.mark.parametrize("loop", ["proactor", "selector"])
def test_asyncio_both_event_loops(loop: str, fake_product_port: int) -> None:
    """ProactorEventLoop (Windows, el del backend) conecta con ConnectEx sin pasar por el gancho de auditoría."""
    if loop == "proactor" and sys.platform != "win32":
        pytest.skip("ProactorEventLoop solo existe en Windows")
    factory = asyncio.ProactorEventLoop if loop == "proactor" else asyncio.SelectorEventLoop  # type: ignore[attr-defined]

    async def main() -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://127.0.0.1:{fake_product_port}/v3/paths/list")
        with pytest.raises(ProductPortTouched):
            await asyncio.open_connection("127.0.0.1", fake_product_port)

    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(main())
    assert len(PRODUCT_PORT_GUARD.hits) == 2, PRODUCT_PORT_GUARD.hits


def test_a_server_of_the_test_itself_is_allowed(fake_product_port: int) -> None:
    """En la CI no hay programa instalado: una prueba puede montar su propio servidor en esos puertos."""
    with socket.create_server(("127.0.0.1", fake_product_port)) as server:
        with socket.create_connection(("127.0.0.1", fake_product_port), timeout=5):
            conn, _ = server.accept()
            conn.close()
    assert PRODUCT_PORT_GUARD.hits == []
    # cerrado el servidor, el puerto vuelve a estar vigilado
    with pytest.raises(ProductPortTouched):
        socket.create_connection(("127.0.0.1", fake_product_port), timeout=5)


def test_a_server_in_a_child_process_is_allowed(fake_product_port: int) -> None:
    """MediaMTX o un backend lanzados por la propia prueba (procesos hijos) también son suyos."""
    pytest.importorskip("psutil")
    code = ("import socket, sys; s = socket.create_server(('127.0.0.1', int(sys.argv[1]))); print('listo', flush=True);"
            " c, _ = s.accept(); c.close()")
    child = subprocess.Popen([sys.executable, "-c", code, str(fake_product_port)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "listo"
        with socket.create_connection(("127.0.0.1", fake_product_port), timeout=5):
            pass
        assert child.wait(10) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)
    assert PRODUCT_PORT_GUARD.hits == []


def test_opencv_capture_stays_closed_and_is_reported(fake_product_port: int) -> None:
    """La instantánea y la salud de imagen leen el RTSP del motor con el FFmpeg de dentro de OpenCV."""
    cv2 = pytest.importorskip("cv2")
    url = f"rtsp://127.0.0.1:{fake_product_port}/cam-0000abcd/sub"
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    assert not cap.isOpened()
    cap.release()
    cap = cv2.VideoCapture()
    assert cap.open(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1000]) is False
    cap.release()
    assert [h.rsplit(" por ", 1)[1] for h in PRODUCT_PORT_GUARD.hits] == ["cv2.VideoCapture",
                                                                          "cv2.VideoCapture.open"]
    # Lo que no es una URL de este equipo (un archivo de vídeo, una cámara de la LAN) no se toca
    PRODUCT_PORT_GUARD.hits.clear()
    assert not PRODUCT_PORT_GUARD.url_refused("tests/assets/people-walking-h264.mp4", "prueba")
    assert not PRODUCT_PORT_GUARD.url_refused(f"rtsp://192.168.1.64:{fake_product_port}/x", "prueba")
    assert PRODUCT_PORT_GUARD.hits == []


@pytest.mark.skipif(sys.platform != "win32", reason="en Linux y macOS un puerto local cerrado ya se rechaza al momento")
def test_fake_engine_closed_ports_are_refused_at_once_on_windows() -> None:
    """Windows tarda 3 s en dar por cerrado un puerto local (5 s con OpenCV): con FakeEngine, la batería de web,
    api, ops, engine y vendors iba 4 minutos más lenta que con los puertos del programa instalado."""
    engine = FakeEngine()
    t0 = time.monotonic()
    with pytest.raises(httpx.ConnectError):
        httpx.post(engine.whep_url(CAM, "sub"), content=b"v=0", timeout=10)
    with pytest.raises(ConnectionRefusedError) as exc:
        socket.create_connection(("127.0.0.1", engine.playback_port), timeout=10)
    assert not isinstance(exc.value, ProductPortTouched)
    cv2 = pytest.importorskip("cv2")
    cap = cv2.VideoCapture()
    assert cap.open(engine.rtsp_read_url(CAM, "sub"), cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000]) is False
    cap.release()
    assert time.monotonic() - t0 < 1.5
    assert PRODUCT_PORT_GUARD.hits == []   # no es ningún fallo de la prueba


def test_a_closed_port_that_someone_took_is_tried_for_real() -> None:
    """Si el sistema ya le ha dado a otro el puerto cerrado de un FakeEngine, la conexión va de verdad."""
    engine = FakeEngine()
    try:
        server = socket.create_server(("127.0.0.1", engine.webrtc_port))
    except OSError:
        pytest.skip("el puerto ya lo tiene otro programa")
    with server, socket.create_connection(("127.0.0.1", engine.webrtc_port), timeout=5):
        conn, _ = server.accept()
        conn.close()
    assert PRODUCT_PORT_GUARD.hits == []


@pytest.mark.parametrize("loop", ["socket", "proactor", "selector"])
def test_an_own_udp_server_on_a_closed_port_number_gets_its_datagrams(loop: str) -> None:
    """Regresión: los puertos UDP de la prueba (vinculados al 0) salen del mismo rango que los TCP que reserva
    FakeEngine. Un responder WS-Discovery que caía en un número de CLOSED_PORTS se quedaba sin datagramas
    (ConnectionRefusedError en sendto, que en UDP nunca pasa) y test_discovery_v2 fallaba al azar."""
    if loop == "proactor" and sys.platform != "win32":
        pytest.skip("ProactorEventLoop solo existe en Windows")
    from tests import fakes

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(5)
        port = int(server.getsockname()[1])
        added = port not in fakes.CLOSED_PORTS
        fakes.CLOSED_PORTS.add(port)
        try:
            if loop == "socket":
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.sendto(b"hola", ("127.0.0.1", port))
            else:
                factory = asyncio.ProactorEventLoop if loop == "proactor" else asyncio.SelectorEventLoop  # type: ignore[attr-defined]

                async def main() -> None:
                    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                        asyncio.DatagramProtocol, family=socket.AF_INET)
                    try:
                        transport.sendto(b"hola", ("127.0.0.1", port))
                        await asyncio.sleep(0.2)
                    finally:
                        transport.close()

                with asyncio.Runner(loop_factory=factory) as runner:
                    runner.run(main())
            data, _ = server.recvfrom(64)
        finally:
            if added:
                fakes.CLOSED_PORTS.discard(port)
    assert data == b"hola"
    assert PRODUCT_PORT_GUARD.hits == []


def test_isolated_settings_never_use_product_ports(tmp_path: Path) -> None:
    s = isolated_settings(tmp_path)
    used = {s.http_port, s.https_port} | {int(a.rpartition(":")[2]) for a in (
        s.mtx_rtsp_address, s.mtx_webrtc_address, s.mtx_webrtc_ice_udp, s.mtx_api_address, s.mtx_playback_address)}
    assert not used & set(product_default_ports()), used
    assert s.mtx_webrtc_ice_tcp == ""
    # y siguen siendo los ajustes del producto (sin .env)
    assert isinstance(s, VmsSettings) and s.site_id == "site-test"
