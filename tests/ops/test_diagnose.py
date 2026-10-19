"""«¿Por qué no conecta?» (CONTRATO §18.11, criterio 7 de B6): cada regla con su doble y EXACTAMENTE 1 intento
con credenciales cuando la contraseña es mala (contador del mock de Hikvision y del doble RTSP)."""
from __future__ import annotations

import inspect
import re
from datetime import timedelta
from typing import Any

import httpx
import pytest

from tests.ops.doubles import ClockDevice, DiagBehavior, DiagClient, IsapiClock, RtspDouble, isapi_app
from tests.ops.test_netroute import WIFI, cable_in, cable_out
from tools.mocks.hikvision import HikvisionMock
from tools.mocks.onvif import OnvifMock
from tools.mocks.rtsp_chaos import RtspChaosServer
from tools.mocks.server import MockHttpServer
from vms.core.interfaces import PathStatus
from vms.core.models import Camera, Device
from vms.ops.diagnose import DiagnoseInput, Diagnoser, sanitize_text
from vms.ops.netroute import Adapter, Addr, NetView
from vms.vendors import client_for

PW = "Buena#1234"


def _dev(**over: Any) -> Device:
    base = {"id": "dev-00000001", "name": "NVR", "vendor": "hikvision", "kind": "nvr", "host": "10.0.0.5",
            "http_port": 80, "rtsp_port": 554, "username": "admin"}
    return Device(**{**base, **over})


def _cam(**over: Any) -> Camera:
    return Camera(**{"id": "cam-00000001", "name": "Cajas", "device_id": "dev-00000001", "channel": 1, **over})


class Rtsp:
    def __init__(self, status: int = 200, codecs: dict[str, str] | None = None, *,
                 locked_minutes: int | None = None, credentialed: int | None = None, error: str = "") -> None:
        self.status = status
        self.codecs = codecs or {}
        self.locked_minutes = locked_minutes      # el equipo dice que el usuario está bloqueado (0 = sin minutos)
        self.credentialed = credentialed          # peticiones con credenciales (None: el doble no lo dice)
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, host: str, port: int, path: str, username: str = "", password: str = "", *,
                       timeout: float = 5.0) -> Any:
        self.calls.append((path, password))
        codec = self.codecs.get(path, "H.264")

        class R:
            pass
        r = R()
        r.status, r.reachable, r.error = self.status, True, self.error   # type: ignore[attr-defined]
        r.video_codec = codec if self.status == 200 else None   # type: ignore[attr-defined]
        if self.locked_minutes is not None:
            r.locked, r.lockout_minutes = True, self.locked_minutes or None   # type: ignore[attr-defined]
        if self.credentialed is not None:
            r.credentialed_requests = self.credentialed   # type: ignore[attr-defined]
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
    # revisión final: mandaba a «reiniciar el servicio desde Estado del sistema», donde no hay ningún botón para eso
    engine = next(s for s in res.steps if s.code == "engine")
    assert "«Estado del sistema»" in engine.action_es and "servicio" not in engine.action_es
    res = await _diag(DiagBehavior(), ready=False).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    pr = next(s for s in res.steps if s.code == "path_ready")
    assert pr.ok is False and "Cajas" in pr.detail_es and "connection refused" in pr.detail_es


def _with_reading(b: DiagBehavior, reading: Any) -> Any:
    """Cliente de diagnóstico cuya hora es `reading()` (función normal o asíncrona)."""
    class WithClock(DiagClient):
        async def device_time(self) -> Any:
            r = reading()
            return await r if inspect.isawaitable(r) else r
    return WithClock(b, PW)


async def test_manual_clock_in_time_is_a_warning_not_the_cause() -> None:
    """Revisión final, cámara del usuario: tras «Poner en hora con este PC» queda en hora puesta a mano (este PC no
    sirve NTP) y con la zona de fábrica (UTC+8) hasta cambiarla en su web. `assess()` lo da como `warning` y el paso
    `clock` lo contaba como fallo: «Hora del equipo» salía «Falla», era la causa probable y nunca salía «Todo
    correcto». Con la hora bien es un aviso; solo un reloj sin poner o muy desajustado es un fallo."""
    from datetime import datetime, timezone

    from vms.core.interfaces import DeviceTime

    # en hora y a mano (como la deja el botón), con el doble ISAPI
    clock_client = IsapiClock(isapi_app(ClockDevice(mode="manual")))
    b = DiagBehavior()
    res = await _diag(b, client=_with_reading(b, clock_client.device_time)).run(
        DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    clock = _step(res, "clock")
    assert clock.ok is True and clock.warning and "puesta a mano" in clock.detail_es and clock.action_es
    assert res.probable_cause_es == "No se ha encontrado ningún problema."
    assert res.summary_es.startswith("Todo correcto") and res.summary_es.endswith(" Hay 1 aviso abajo.")

    # además con la zona de fábrica distinta de la del PC: sigue siendo un solo aviso
    pc_off = datetime.now().astimezone().utcoffset() or timedelta(0)
    zone = timedelta(hours=8) if pc_off != timedelta(hours=8) else timedelta(hours=1)

    def in_zone() -> DeviceTime:
        now = datetime.now(timezone.utc)
        return DeviceTime(device_time=now.astimezone(timezone(zone)), measured_at=now, time_mode="manual",
                          utc_offset_s=int(zone.total_seconds()), source="isapi")
    b = DiagBehavior()
    res = await _diag(b, client=_with_reading(b, in_zone)).run(
        DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    clock = _step(res, "clock")
    assert clock.ok is True and clock.warning and "zona horaria" in clock.detail_es
    assert res.probable_cause_es == "No se ha encontrado ningún problema."
    assert res.summary_es.startswith("Todo correcto") and res.summary_es.endswith(" Hay 1 aviso abajo.")

    # un reloj sin poner (01-01-1970) sigue siendo un fallo y la causa probable
    def never_set() -> DeviceTime:
        return DeviceTime(device_time=datetime(1970, 1, 1, 0, 7, 9, tzinfo=timezone.utc),
                          measured_at=datetime.now(timezone.utc), time_mode="manual", source="isapi")
    b = DiagBehavior()
    res = await _diag(b, client=_with_reading(b, never_set)).run(
        DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    clock = _step(res, "clock")
    assert clock.ok is False and not clock.warning and "no tiene la hora puesta" in clock.detail_es
    assert res.probable_cause_es == clock.detail_es and "Todo correcto" not in res.summary_es


async def test_restricted_user_clock_read_says_what_to_do() -> None:
    """Revisión final: un usuario sin permiso para ver la hora (ISAPI 403 `lowPrivilege`) daba «No se pudo leer la
    hora: El usuario de … no tiene permiso para /ISAPI/System/time», con la ruta técnica y sin qué hacer, mientras
    «Hora de los equipos» sí lo explicaba."""
    from vms.vendors.errors import DevicePermissionDenied

    def denied() -> Any:
        raise DevicePermissionDenied("10.0.0.5", "/ISAPI/System/time")
    b = DiagBehavior()
    res = await _diag(b, client=_with_reading(b, denied)).run(
        DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    clock = _step(res, "clock")
    assert clock.ok is None and "/ISAPI" not in clock.detail_es and "no tiene permiso" in clock.detail_es
    assert "apartado de usuarios" in clock.action_es and "«Editar»" in clock.action_es
    assert res.probable_cause_es == "No se ha encontrado ningún problema.", "no es la causa"


async def test_rtsp_rejecting_what_the_api_accepted_does_not_blame_the_password() -> None:
    """8/10, cámara del usuario: la API aceptó la contraseña y el vídeo (RTSP) la rechazó. El paso decía «El vídeo
    (RTSP) rechaza el usuario o la contraseña» justo debajo de «Sin riesgo de bloqueo: la contraseña es correcta», y
    el usuario la cambiaba. Si la API la acaba de aceptar, es correcta: lo que falla es el vídeo."""
    rtsp = Rtsp(status=401)
    b = DiagBehavior()
    res = await _diag(b, rtsp=rtsp).run(DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    st = _steps(res)
    assert st["auth"] is True and st["lockout"] is True and st["rtsp_describe"] is False
    step = _step(res, "rtsp_describe")
    assert step.detail_es == "El equipo acepta el usuario y la contraseña, pero el vídeo (RTSP) los rechaza."
    assert step.action_es.startswith("No cambies la contraseña") and "permiso para ver el vídeo" in step.action_es
    assert "una sola vez" in step.action_es and res.probable_cause_es == step.detail_es
    assert b.attempts == 1 and len(rtsp.calls) == 1, "un intento por la API y uno por RTSP, sin el subflujo"


async def test_rtsp_lock_and_permission_say_what_to_do() -> None:
    """Un bloqueo que dice el RTSP no es «contraseña mala», y un 403 (sin permiso) salía con el código y sin qué hacer."""
    res = await _diag(DiagBehavior(), rtsp=Rtsp(status=401, locked_minutes=0)).run(
        DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    step = _step(res, "rtsp_describe")
    assert step.ok is False and "bloqueado" in step.detail_es and step.action_es.startswith("Espera 30 minutos")
    res = await _diag(DiagBehavior(), rtsp=Rtsp(status=403)).run(
        DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    step = _step(res, "rtsp_describe")
    assert step.ok is False and "no tiene permiso" in step.detail_es and "403" not in step.detail_es
    assert "apartado de usuarios" in step.action_es


async def test_onvif_answering_any_password_does_not_vouch_for_rtsp() -> None:
    """Muchas cámaras ONVIF genéricas tienen la autenticación ONVIF desactivada: contestan con cualquier contraseña.
    Con una MALA, el paso del vídeo no puede decir que la contraseña es correcta ni que no se cambie: solo la API de
    la marca (ISAPI, Dahua) usa las mismas cuentas que el RTSP. Con el cliente ONVIF y la prueba RTSP de verdad."""
    async with RtspChaosServer(password="Otra#Pass:2") as rtsp:      # la contraseña de verdad es otra
        onv = OnvifMock(password=PW, auth_disabled=True, rtsp_base=f"rtsp://127.0.0.1:{rtsp.port}")

        async def tcp(host: str, port: int, timeout: float) -> bool:
            return port in (80, rtsp.port)

        async def http_probe(host: str, port: int, https: bool, timeout: float) -> int | None:
            return 401

        diag = Diagnoser(lambda d, p: client_for(d, p, timeout=3.0, transport=httpx.ASGITransport(app=onv.app)),
                         tcp_check=tcp, http_probe=http_probe)
        dev = _dev(vendor="onvif", kind="camera", host="127.0.0.1", rtsp_port=rtsp.port)
        res = await diag.run(DiagnoseInput(dev, "Mala#1", "dev-00000001",
                                           [_cam(main_path="/Streaming/Channels/101", has_sub=False)]))
    st = _steps(res)
    assert st["auth"] is True and st["rtsp_describe"] is False and onv.rejected == 0   # ONVIF contestó a todo
    step = _step(res, "rtsp_describe")
    assert step.detail_es == "El vídeo (RTSP) rechaza el usuario o la contraseña."
    text = f"{step.detail_es} {step.action_es} {res.summary_es}"
    assert "es correcta" not in text and "No cambies la contraseña" not in text and "acaba de aceptarla" not in text
    assert rtsp.stats.credentialed == 1 and rtsp.stats.rejected == 1


async def test_rtsp_401_without_sending_the_password_is_not_a_rejection() -> None:
    """Con la contraseña aceptada por la API, un 401 del RTSP sin haber mandado credenciales (el equipo pide una
    autenticación que no se sabe responder) decía que el vídeo «los rechaza» y que no se cambiara la contraseña."""
    rtsp = Rtsp(status=401, credentialed=0, error="El equipo pide una autenticación RTSP que no se reconoce")
    res = await _diag(DiagBehavior(), rtsp=rtsp).run(DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", [_cam()]))
    step = _step(res, "rtsp_describe")
    assert step.ok is False and step.detail_es == "El equipo pide una autenticación RTSP que no se reconoce."
    assert "rechaza" not in step.detail_es and "«digest»" in step.action_es
    assert "No cambies la contraseña" not in step.action_es


@pytest.mark.parametrize("allow_basic", [False, True])
async def test_basic_only_rtsp_is_not_a_wrong_password_and_honours_allow_basic(allow_basic: bool) -> None:
    """Un RTSP que solo ofrece Basic daba «El vídeo (RTSP) rechaza el usuario o la contraseña» sin haberla mandado,
    y fallaba aunque el equipo tuviera «Permitir autenticación Basic»: el diagnóstico no pasaba ese permiso a la
    prueba RTSP, al revés que «Probar conexión»."""
    async with RtspChaosServer(password=PW, scenario="basic-only") as rtsp:
        async def tcp(host: str, port: int, timeout: float) -> bool:
            return port == rtsp.port

        dev = _dev(vendor="generic", kind="camera", host="127.0.0.1", rtsp_port=rtsp.port, allow_basic=allow_basic)
        res = await Diagnoser(lambda d, p: DiagClient(DiagBehavior(), p), tcp_check=tcp).run(
            DiagnoseInput(dev, PW, "dev-00000001", [_cam(main_path="/stream", has_sub=False)]))
    step = _step(res, "rtsp_describe")
    if allow_basic:
        assert step.ok is True and _steps(res)["codec"] is True, step
        assert rtsp.stats.schemes == ["basic"] and rtsp.stats.rejected == 0
    else:
        assert step.ok is False and "solo admite autenticación Basic" in step.detail_es
        assert "rechaza" not in step.detail_es and "«Permitir autenticación Basic»" in step.action_es
        assert rtsp.stats.credentialed == 0, "la contraseña no se manda en claro sin permiso"


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


async def test_user_camera_rfc2069_rtsp_challenge_passes_the_video_step() -> None:
    """8/10, cámara del usuario (Hikvision DS-2CD1741FWD-IZ, firmware V5.5.0): con la contraseña buena, el paso del
    vídeo decía que RTSP rechazaba el usuario o la contraseña. Su RTSP reta sin qop ni algorithm y daba 401 a una
    respuesta con «algorithm=MD5». Con el cliente ISAPI y la prueba RTSP de verdad, contra un doble que hace lo
    mismo que esa cámara."""
    mock = HikvisionMock(kind="camera", password=PW, model="DS-2CD1741FWD-IZ", firmware="V5.5.0")
    srv = MockHttpServer(mock.app).start()
    try:
        async with RtspChaosServer(password=PW, scenario="digest-no-algorithm") as rtsp:
            dev = _dev(host="127.0.0.1", http_port=srv.port, rtsp_port=rtsp.port, kind="camera")
            res = await Diagnoser(lambda d, p: client_for(d, p, timeout=3.0)).run(
                DiagnoseInput(dev, PW, "dev-00000001", [_cam()]))
    finally:
        srv.stop()
    st = _steps(res)
    assert st["auth"] and st["lockout"] and st["rtsp_describe"] and st["codec"], res.steps
    assert rtsp.stats.rejected == 0 and mock.auth.rejected_credentials == 0


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


# --------------------------------------------------------------------------- equipo guardado sin cámaras
def _step(res: Any, code: str) -> Any:
    return next(s for s in res.steps if s.code == code)


async def test_saved_device_without_cameras_fails_with_what_to_do() -> None:
    """Lo que pasó el 6/10 (vms.log): la cámara se guardó con la contraseña mala, así que no se importó su canal, y
    corregirla en «Editar» no lo importa. Con el equipo sin cámaras, «¿Por qué no conecta?» decía «Este equipo aún
    no tiene cámaras…» sin comprobar y sin «Qué hacer» y, con el resto bien, «Todo correcto… el vídeo llega al VMS»."""
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(kind="camera"), PW, "dev-00000001", []))
    pr = _step(res, "path_ready")
    assert pr.ok is False and "no se ve ni se graba" in pr.detail_es
    assert "«Canales»" in pr.action_es and "«Editar»" in pr.action_es
    assert "Todo correcto" not in res.summary_es and "llega al VMS" not in res.summary_es
    assert "«Canales»" in res.summary_es and res.probable_cause_es == pr.detail_es
    # grabador: habla de sus canales
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(kind="nvr"), PW, "dev-00000001", []))
    pr = _step(res, "path_ready")
    assert pr.ok is False and "ninguno de sus canales" in pr.detail_es and "los canales que quieras" in pr.action_es
    # marca sin API: no hay «Canales», se da de alta a mano
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(kind="camera", vendor="generic"), PW, "dev-00000001",
                                                        []))
    pr = _step(res, "path_ready")
    assert pr.ok is False and pr.action_es.startswith("Pulsa «Añadir cámara manual»")


async def test_wrong_password_and_no_cameras_blames_the_password_first() -> None:
    """El caso real de las 20:26 del 6/10: contraseña mala y sin cámaras. La causa es la contraseña; la falta de
    cámaras se cuenta como otro aviso."""
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(kind="camera"), "Mala#000", "dev-00000001", []))
    st = _steps(res)
    assert st["auth"] is False and st["path_ready"] is False
    assert "contraseña" in res.probable_cause_es and "avisos más abajo" in res.summary_es


async def test_unsaved_form_without_cameras_is_not_a_failure() -> None:
    """Desde el formulario de alta el equipo aún no está guardado: no es un fallo, pero tampoco se puede decir que
    el vídeo llegue al VMS."""
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(kind="camera"), PW))
    pr = _step(res, "path_ready")
    assert pr.ok is None and "aún no está guardado" in pr.detail_es
    assert res.probable_cause_es == "No se ha encontrado ningún problema."
    assert "llega al VMS" not in res.summary_es


async def test_summary_only_says_the_video_arrives_when_it_was_checked() -> None:
    """Sin estado de las rutas del motor (`paths_status` None) el resumen decía igual «Todo correcto… el vídeo llega
    al VMS»."""
    d = _diag(DiagBehavior())
    d.paths_status = None
    res = await d.run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    assert _step(res, "path_ready").ok is None
    assert "Todo correcto" not in res.summary_es and "no se ha podido comprobar si llega el vídeo" in res.summary_es
    assert "Qué hacer:" in res.summary_es
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam()]))
    assert res.summary_es.startswith("Todo correcto") and "llega al VMS" in res.summary_es


async def test_disabled_cameras_are_not_told_to_wait() -> None:
    """Una cámara desactivada no tiene ruta en el motor (`vms/core/sources.py`): «espera un minuto, el VMS reintenta
    solo» no era verdad."""
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam(enabled=False)]))
    pr = _step(res, "path_ready")
    assert pr.ok is False and "desactivada" in pr.detail_es and "«Activada»" in pr.action_es
    assert "espera un minuto" not in pr.action_es
    # revisión final: la misma pantalla que nombra Reproducción para lo mismo («Equipos y monitores»)
    assert pr.action_es.startswith("En «Equipos y monitores»")
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(), PW, "dev-00000001", [_cam(enabled=False)],
                                                        camera_id="cam-00000001"))
    assert _step(res, "path_ready").detail_es.startswith("Esta cámara está desactivada")
    # con una activa y otra desactivada, solo cuenta la activa
    cams = [_cam(), _cam(id="cam-00000002", name="Almacén", channel=2, enabled=False)]
    res = await _diag(DiagBehavior()).run(DiagnoseInput(_dev(), PW, "dev-00000001", cams))
    assert _step(res, "path_ready").ok is True


# --------------------------------------------------------------------------- red de este PC (paso `route`)
CAM_DEV = {"host": "192.168.1.64", "kind": "camera"}


def _net(view: Any) -> Any:
    async def probe(host: str) -> Any:
        return view
    return probe


async def test_unplugged_pc_cable_is_the_probable_cause() -> None:
    """PC de la tienda, 8/10: con el cable de «Ethernet» desconectado Windows buscaba la cámara por «Wi-Fi» (las
    dos en 192.168.1.0/24). El diagnóstico decía «El equipo no responde en la red» y mandaba revisar el cable de la
    cámara."""
    d = _diag(DiagBehavior(), ports={})
    d.net_probe = _net(cable_out())
    res = await d.run(DiagnoseInput(_dev(**CAM_DEV), PW, "dev-00000001", [_cam()]))
    assert [s.code for s in res.steps][:2] == ["route", "ping"] and res.steps[0].ok is False
    assert "«Ethernet»" in res.probable_cause_es and "desconectada" in res.probable_cause_es
    assert "Conecta el cable de red de «Ethernet»" in res.summary_es and _steps(res)["ping"] is False


async def test_both_cards_up_and_camera_answers_is_only_a_warning() -> None:
    """El mismo PC con el cable puesto: funciona, pero «Wi-Fi» también está en la red de la cámara."""
    d = _diag(DiagBehavior())
    d.net_probe = _net(cable_in())
    res = await d.run(DiagnoseInput(_dev(**CAM_DEV), PW, "dev-00000001", [_cam()]))
    route = _step(res, "route")
    assert route.ok is True and route.warning and "«Wi-Fi»" in route.detail_es and route.action_es
    assert res.probable_cause_es == "No se ha encontrado ningún problema."
    assert res.summary_es.startswith("Todo correcto") and res.summary_es.endswith(" Hay 1 aviso abajo.")


async def test_wifi_route_is_a_warning_and_the_camera_check_stays_the_cause() -> None:
    """Las dos con enlace pero Windows sale por «Wi-Fi» y la cámara no contesta: puede ser la ruta o que esté
    apagada; la causa probable sigue siendo la de siempre y la ruta va como aviso."""
    d = _diag(DiagBehavior(), ports={})
    d.net_probe = _net(cable_in("192.168.1.141"))
    res = await d.run(DiagnoseInput(_dev(**CAM_DEV), PW, "dev-00000001", [_cam()]))
    route = _step(res, "route")
    assert route.ok is None and route.warning and "desactiva un momento «Wi-Fi»" in route.action_es
    assert "no responde en la red" in res.probable_cause_es and "(Hay 1 aviso más abajo.)" in res.summary_es


async def test_pc_network_probe_failure_is_not_fatal() -> None:
    async def broken(host: str) -> Any:
        raise OSError("psutil roto")

    d = _diag(DiagBehavior())
    d.net_probe = broken
    res = await d.run(DiagnoseInput(_dev(**CAM_DEV), PW, "dev-00000001", [_cam()]))
    assert _step(res, "route").ok is None and _steps(res)["auth"] is True
    assert res.probable_cause_es == "No se ha encontrado ningún problema."


async def test_route_step_only_with_a_lan_ip() -> None:
    """Con un nombre DNS o 127.0.0.1 no se mira la red del PC (ni se resuelve el nombre)."""
    calls: list[str] = []

    async def probe(host: str) -> Any:
        calls.append(host)
        return cable_out()

    for host in ("127.0.0.1", "cam-caja.tienda.local"):
        d = _diag(DiagBehavior())
        d.net_probe = probe
        res = await d.run(DiagnoseInput(_dev(host=host), PW, "dev-00000001", [_cam()]))
        assert "route" not in _steps(res)
    assert calls == []


async def test_llm_never_sees_card_names_or_networks() -> None:
    """Al LLM no van las IP del PC, la red escrita como «192.168.1.x» ni un nombre de tarjeta puesto a mano."""
    seen: list[str] = []

    async def rewriter(summary: str, steps: list[Any]) -> str:
        seen.append(summary + " " + " ".join(s.detail_es + " " + s.action_es for s in steps))
        return "Revisa el cable."

    custom = Adapter("Red Caja Gran Vía", False, (Addr("192.168.1.80", 24),))
    for view in (NetView((custom, WIFI), "192.168.1.141"), cable_in("192.168.1.141")):
        d = _diag(DiagBehavior(), ports={})
        d.net_probe, d.rewriter = _net(view), rewriter
        res = await d.run(DiagnoseInput(_dev(**CAM_DEV), PW, "dev-00000001", [_cam()]))
        assert res.llm_used and _step(res, "route").detail_es
    sent = " ".join(seen)
    assert "Gran Vía" not in sent and not re.search(r"\d+\.\d+\.\d+\.", sent), sent
    assert "«Wi-Fi»" in sent           # los nombres de siempre de Windows sí: dicen qué cable revisar


async def test_service_diagnoser_reads_this_pc_network(api: Any) -> None:
    from vms.ops import netroute
    assert api.app.state.ops.diagnoser().net_probe is netroute.probe
