"""«¿Por qué no conecta?» (CONTRATO §18.11, criterio 7 de B6): cada regla con su doble y EXACTAMENTE 1 intento
con credenciales cuando la contraseña es mala (contador del mock de Hikvision y del doble RTSP)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from tests.ops.doubles import ClockDevice, DiagBehavior, DiagClient, IsapiClock, RtspDouble, isapi_app
from tools.mocks.hikvision import HikvisionMock
from tools.mocks.server import MockHttpServer
from vms.core.interfaces import PathStatus
from vms.core.models import Camera, Device
from vms.ops.diagnose import DiagnoseInput, Diagnoser, sanitize_text
from vms.vendors import client_for

PW = "Buena#1234"


def _dev(**over: Any) -> Device:
    base = {"id": "dev-00000001", "name": "NVR", "vendor": "hikvision", "kind": "nvr", "host": "10.0.0.5",
            "http_port": 80, "rtsp_port": 554, "username": "admin"}
    return Device(**{**base, **over})


def _cam(**over: Any) -> Camera:
    return Camera(**{"id": "cam-00000001", "name": "Cajas", "device_id": "dev-00000001", "channel": 1, **over})


class Rtsp:
    def __init__(self, status: int = 200, codecs: dict[str, str] | None = None) -> None:
        self.status = status
        self.codecs = codecs or {}
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, host: str, port: int, path: str, username: str = "", password: str = "", *,
                       timeout: float = 5.0) -> Any:
        self.calls.append((path, password))
        codec = self.codecs.get(path, "H.264")

        class R:
            pass
        r = R()
        r.status, r.reachable, r.error = self.status, True, ""   # type: ignore[attr-defined]
        r.video_codec = codec if self.status == 200 else None   # type: ignore[attr-defined]
        return r


def _diag(behavior: DiagBehavior, *, ports: dict[int, bool] | None = None, http: int | None = 401,
          rtsp: Rtsp | None = None, engine: bool = True, ready: bool = True, client: Any = None) -> Diagnoser:
    ports = ports if ports is not None else {80: True, 554: True}

    async def tcp(host: str, port: int, timeout: float) -> bool:
        return ports.get(port, False)

    async def http_probe(host: str, port: int, https: bool, timeout: float) -> int | None:
        return http

    async def running() -> bool:
        return engine

    async def paths() -> dict[str, PathStatus]:
        return {"cam-00000001/main": PathStatus(name="cam-00000001/main", ready=ready,
                                                last_error="" if ready else "connection refused")}

    factory = (lambda d, p: client) if client is not None else (lambda d, p: DiagClient(behavior, p))
    return Diagnoser(factory, engine_running=running, paths_status=paths, tcp_check=tcp, http_probe=http_probe,
                     rtsp_probe=rtsp or Rtsp())


def _steps(res: Any) -> dict[str, bool | None]:
    return {s.code: s.ok for s in res.steps}


async def test_all_good() -> None:
    b = DiagBehavior()
    res = await _diag(b).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    st = _steps(res)
    assert [s.code for s in res.steps] == ["ping", "tcp_http", "tcp_rtsp", "http_response", "auth", "lockout",
                                           "rtsp_describe", "codec", "clock", "engine", "path_ready"]
    assert all(st[c] for c in ("ping", "tcp_http", "tcp_rtsp", "http_response", "auth", "lockout", "rtsp_describe",
                               "codec", "engine", "path_ready"))
    assert st["clock"] is None, "la marca de prueba no lee la hora"
    assert res.probable_cause_es == "No se ha encontrado ningún problema." and b.attempts == 1


async def test_unreachable_stops_early() -> None:
    b = DiagBehavior()
    res = await _diag(b, ports={}).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    st = _steps(res)
    assert st["ping"] is False and st["auth"] is None and b.attempts == 0
    assert "no responde en la red" in res.probable_cause_es and "cable" in res.summary_es


async def test_rtsp_port_closed_and_http_closed() -> None:
    res = await _diag(DiagBehavior(), ports={80: True}).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    st = _steps(res)
    assert st["tcp_rtsp"] is False and st["rtsp_describe"] is None and "RTSP" in res.probable_cause_es
    res = await _diag(DiagBehavior(), ports={554: True}).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    assert _steps(res)["tcp_http"] is False and _steps(res)["auth"] is None


async def test_http_without_web_answer() -> None:
    res = await _diag(DiagBehavior(), http=None).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    assert _steps(res)["http_response"] is False and "HTTPS" in res.steps[3].action_es


async def test_bad_password_is_tried_exactly_once_and_lockout_is_explained() -> None:
    b = DiagBehavior()
    rtsp = Rtsp()
    res = await _diag(b, rtsp=rtsp).run(DiagnoseInput(_dev(), "Mala#000", "dev-00000001", [_cam()]))
    st = _steps(res)
    assert st["auth"] is False and st["lockout"] is False
    assert b.attempts == 1 and rtsp.calls == [], "ni un intento más, tampoco por RTSP"
    lock = next(s for s in res.steps if s.code == "lockout")
    assert "5 intentos" in lock.detail_es and "30 minutos" in lock.detail_es and "Hikvision" in lock.detail_es
    assert st["rtsp_describe"] is None and "contraseña" in res.probable_cause_es


async def test_rtsp_wrong_path_and_h265_substream() -> None:
    res = await _diag(DiagBehavior(), rtsp=Rtsp(status=404)).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    assert _steps(res)["rtsp_describe"] is False and "ruta" in res.probable_cause_es
    rtsp = Rtsp(codecs={"/Streaming/Channels/102": "H.265"})
    res = await _diag(DiagBehavior(), rtsp=rtsp).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    codec = next(s for s in res.steps if s.code == "codec")
    assert codec.ok is False and "H.265" in codec.detail_es and "H.264" in codec.action_es
    assert [p for p, _ in rtsp.calls] == ["/Streaming/Channels/101", "/Streaming/Channels/102"]


async def test_clock_engine_and_path_rules() -> None:
    clock_client = IsapiClock(isapi_app(ClockDevice(skew=timedelta(hours=2))))
    b = DiagBehavior()

    class WithClock(DiagClient):
        async def device_time(self) -> Any:
            return await clock_client.device_time()

    d = _diag(b, engine=False, ready=False, client=WithClock(b, PW))
    res = await d.run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    st = _steps(res)
    assert st["clock"] is False and "adelantada" in next(s.detail_es for s in res.steps if s.code == "clock")
    assert st["engine"] is False and st["path_ready"] is None
    res = await _diag(DiagBehavior(), ready=False).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    pr = next(s for s in res.steps if s.code == "path_ready")
    assert pr.ok is False and "Cajas" in pr.detail_es and "connection refused" in pr.detail_es


async def test_generic_rtsp_device_tries_credentials_once_via_rtsp() -> None:
    rtsp = Rtsp(status=401)
    b = DiagBehavior()
    dev = _dev(vendor="generic", kind="camera")
    res = await _diag(b, rtsp=rtsp).run(DiagnoseInput(dev, "Mala#000", "dev-00000001", [_cam(main_path="/live")]))
    assert _steps(res)["auth"] is None and b.attempts == 0 and len(rtsp.calls) == 1
    assert _steps(res)["rtsp_describe"] is False


async def test_llm_only_rewrites_the_summary_without_ip_or_secrets() -> None:
    seen: list[str] = []

    async def rewriter(summary: str, steps: list[Any]) -> str:
        seen.append(summary + " ".join(s.detail_es + s.action_es for s in steps))
        return "La cámara no contesta: revisa el cable en 10.0.0.5 con admin:secreto."

    d = _diag(DiagBehavior(), ports={})
    d.rewriter = rewriter
    res = await d.run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    assert res.llm_used and "10.0.0.5" not in res.summary_es and "<IP>" in res.summary_es
    assert all("10.0.0.5" not in s and PW not in s for s in seen)
    assert sanitize_text("rtsp://admin:pw@10.1.2.3:554/x") == "rtsp://***:***@<IP>:554/x"


class CountCredentials:
    """Envuelve el mock y cuenta las peticiones que llevan credenciales (cabecera Authorization)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.credentialed = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and any(k.lower() == b"authorization" for k, _ in scope.get("headers", [])):
            self.credentialed += 1
        await self.app(scope, receive, send)


@pytest.fixture
async def hik() -> Any:
    mock = HikvisionMock(kind="camera", password=PW)
    counter = CountCredentials(mock.app)
    srv = MockHttpServer(counter).start()
    rtsp = await RtspDouble(password=PW).start()
    yield counter, srv, rtsp
    await rtsp.stop()
    srv.stop()


async def test_real_isapi_client_against_mock_counts_one_credentialed_attempt(hik: Any) -> None:
    """Con el cliente ISAPI real (B5) contra el mock de Hikvision y el doble RTSP: contraseña mala = exactamente
    1 petición con credenciales en todo el diagnóstico (ninguna por RTSP)."""
    counter, srv, rtsp = hik
    dev = _dev(host="127.0.0.1", http_port=srv.port, rtsp_port=rtsp.port, kind="camera")
    diag = Diagnoser(lambda d, p: client_for(d, p, timeout=3.0))
    res = await diag.run(DiagnoseInput(dev, "Mala#000", "dev-00000001", [_cam()]))
    assert _steps(res)["auth"] is False
    assert counter.credentialed == 1 and rtsp.credentialed == 0
    res = await diag.run(DiagnoseInput(dev, PW, "dev-00000001", [_cam()]))
    st = _steps(res)
    assert st["ping"] and st["auth"] and st["rtsp_describe"] and st["codec"], res.steps


async def test_device_without_time_1970_does_not_break_the_diagnosis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cámara sin hora (01-01-1970, zona de fábrica UTC+8): en Windows `astimezone()` de ese instante lanza
    OSError (localtime antes de 1970) y el diagnóstico daba «Error interno del servidor»."""
    from datetime import datetime, timezone

    from vms.core.interfaces import DeviceTime
    from vms.ops.health import clock as health_clock

    class WinDatetime(datetime):
        def astimezone(self, tz: Any = None) -> Any:  # type: ignore[override]
            if tz is None and self.timestamp() < 86400:
                raise OSError(22, "Invalid argument")
            return super().astimezone(tz)

    t = WinDatetime(1970, 1, 1, 0, 7, 9, tzinfo=timezone(timedelta(hours=8)))
    dt = DeviceTime(device_time=t, measured_at=datetime.now(timezone.utc), utc_offset_s=8 * 3600)
    chk = health_clock.device_check("dev-1", None, dt, 2.0, 30.0)
    assert chk.status == "critical"

    b = DiagBehavior()

    class Broken(DiagClient):
        async def device_time(self) -> Any:
            raise OSError(22, "Invalid argument")

    res = await _diag(b, client=Broken(b, PW)).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    assert _steps(res)["clock"] is None and _steps(res)["auth"] is True
