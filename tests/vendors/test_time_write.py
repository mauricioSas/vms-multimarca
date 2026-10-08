"""«Poner en hora con este PC» (capacidad `time_write`, CONTRATO §18.3) con los simuladores de ISAPI y ONVIF.

Reproduce la cámara real del usuario (Hikvision DS-2CD1741FWD-IZ, firmware V5.5.0): hora puesta a mano, zona de
fábrica CST-8:00:00 (UTC+8) y el reloj vuelto a 01-01-1970 00:07:29 hora local tras encenderse (UTC antes de
1970). Antes no había forma de escribir la hora: solo `device_time()`. Nunca se habla con una cámara real.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, request_response

from tests.vendors.conftest import TEST_PASSWORD, asgi, make_device
from tools.mocks.hikvision import NS, XML, HikvisionMock, _status
from tools.mocks.onvif import OnvifMock
from vms.core.errors import DeviceAuthFailed, DeviceProtocolError, DeviceUnsupported
from vms.core.interfaces import DeviceClockWriter
from vms.vendors import client_for
from vms.vendors._http import not_supported
from vms.vendors.errors import DeviceClockManaged, DevicePermissionDenied, DeviceTimeUnverified, DeviceWriteDenied

UTC8 = timezone(timedelta(hours=8))
REAL_CAMERA_TIME = datetime(1970, 1, 1, 0, 7, 29, tzinfo=UTC8)   # lo que se leyó en la cámara del usuario


def _offset_to(instant: datetime) -> float:
    return (instant - datetime.now(timezone.utc)).total_seconds()


def _hik_1970(**kw: Any) -> HikvisionMock:
    kw.setdefault("password", TEST_PASSWORD)
    kw.setdefault("time_mode", "manual")
    return HikvisionMock(kind="camera", tz_hours=8, clock_offset_s=_offset_to(REAL_CAMERA_TIME), **kw)


def _replace_route(app: Any, path: str, endpoint: Any, methods: list[str]) -> None:
    for r in app.inner.routes:
        if isinstance(r, Route) and r.path == path:
            r.endpoint, r.app, r.methods = endpoint, request_response(endpoint), set(methods)
            return
    raise AssertionError(f"ruta {path} no encontrada en el simulador")


# --------------------------------------------------------------------------- ISAPI
async def test_hikvision_1970_is_set_to_pc_time_keeping_its_zone() -> None:
    mock = _hik_1970()
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    assert isinstance(client, DeviceClockWriter)
    try:
        before = await client.device_time()   # type: ignore[attr-defined]
        assert before.device_time.year == 1970 and before.skew_s < -1.7e9
        after = await client.set_device_time()
    finally:
        await client.aclose()
    assert [r for r in mock.requests if r.startswith("PUT")] == ["PUT /ISAPI/System/time"]
    body = mock.time_puts[0]
    assert "<timeMode>manual</timeMode>" in body and "<timeZone>CST-8:00:00</timeZone>" in body, body
    assert f'xmlns="{NS}"' in body, "se reenvía el XML del propio equipo (mismo espacio de nombres)"
    sent = datetime.fromisoformat(re.search(r"<localTime>([^<]+)</localTime>", body).group(1))  # type: ignore[union-attr]
    assert sent.utcoffset() == timedelta(hours=8), "la hora va en la zona del equipo, que no se cambia"
    assert abs((sent - datetime.now(timezone.utc)).total_seconds()) < 2
    assert abs(after.skew_s) < 2 and after.utc_offset_s == 8 * 3600 and after.time_mode == "manual"


@pytest.mark.parametrize("mode", ["NTP", "SDK", "platform", "timecorrect", "satellite", "ONVIF"])
async def test_hikvision_refuses_when_another_system_owns_the_clock(mode: str) -> None:
    """HikCentral/iVMS-4200 (SDK, platform), el grabador (timecorrect), GPS o un servidor NTP: no se pelea."""
    mock = _hik_1970(time_mode=mode)
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceClockManaged) as err:
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert err.value.time_mode == mode and mock.time_puts == []
    if mode == "NTP":
        assert err.value.ntp_server == "pool.ntp.org"


async def test_read_only_user_is_write_denied_not_a_bad_password() -> None:
    """El usuario del VMS es de solo lectura (ALTA-EQUIPOS.md): el 403 `lowPrivilege` del firmware lleva también
    «Invalid Operation» y antes se tomaba por «el equipo no admite /ISAPI/System/time»; tampoco puede contar como
    contraseña mala. Con el administrador temporal sí se escribe, y solo por Digest."""
    mock = _hik_1970(username="visor", extra_users={"admin": "Adm#Temporal:9"}, write_users={"admin"})
    client = client_for(make_device("hikvision", username="visor"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceWriteDenied):
            await client.set_device_time()   # type: ignore[attr-defined]
        still = await client.device_time()   # type: ignore[attr-defined]
        assert still.device_time.year == 1970, "el cliente no queda fundido: la lectura sigue funcionando"
        after = await client.set_device_time("admin", "Adm#Temporal:9")   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert abs(after.skew_s) < 2 and len(mock.time_puts) == 2
    assert mock.auth.rejected_credentials == 0, "ningún intento fallido de contraseña"


async def test_read_only_user_answered_with_401_is_write_denied_and_does_not_fuse() -> None:
    """Revisión: no se sabe si el firmware real contesta a la escritura de un usuario sin permiso con 403
    `lowPrivilege` o con 401. Con 401 se tomaba por contraseña mala (cliente fundido y 30 min sin leer), aunque la
    misma contraseña acababa de valer para leer la hora en el mismo cliente."""
    mock = _hik_1970(username="visor", extra_users={"admin": "Adm#Temporal:9"}, write_users={"admin"},
                     write_denied_status=401)
    client: Any = client_for(make_device("hikvision", username="visor"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceWriteDenied):
            await client.set_device_time()
        assert (await client.device_time()).device_time.year == 1970, "el cliente no queda fundido"
    finally:
        await client.aclose()
    assert len(mock.time_puts) == 1


async def test_low_privilege_on_a_read_is_not_a_bad_password() -> None:
    """Revisión: un 403 `lowPrivilege` en una LECTURA (hora, instantánea) de un usuario restringido pasó a ser
    `DeviceAuthFailed`, y B6 pausaba 30 min la API del equipo diciendo que «rechaza la contraseña guardada». Vuelve a
    ser `DeviceUnsupported` (como antes) y el cliente sigue sirviendo."""
    mock = _hik_1970()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.query_params.get("denied"):     # este usuario no tiene permiso para esta lectura
            return Response(_status(4, "lowPrivilege", "Invalid Operation", request.url.path), 403, media_type=XML)
        return await real_time(request)

    _replace_route(mock.app, "/ISAPI/System/time", time_, ["GET", "PUT"])
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceUnsupported) as err:
            await client.http.get("/ISAPI/System/time", params={"denied": "1"})
        assert isinstance(err.value, DevicePermissionDenied) and not isinstance(err.value, DeviceAuthFailed)
        assert (await client.device_time()).device_time.year == 1970, "el cliente no queda fundido"
    finally:
        await client.aclose()


async def test_user_who_cannot_even_read_the_time_is_write_denied() -> None:
    """Un usuario sin permiso ni para leer la hora salía como «la cámara no deja cambiar la hora desde el programa»
    (501): lo que hay que hacer es lo mismo que con el de solo lectura, escribir el administrador."""
    mock = _hik_1970()

    async def time_(request: Request) -> Response:
        return Response(_status(4, "lowPrivilege", "Invalid Operation", request.url.path), 403, media_type=XML)

    _replace_route(mock.app, "/ISAPI/System/time", time_, ["GET", "PUT"])
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceWriteDenied):
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()


def test_low_privilege_is_not_not_supported() -> None:
    body = _status(4, "lowPrivilege", "Invalid Operation", "/ISAPI/System/time")
    assert not not_supported(httpx.Response(403, content=body.encode()))
    assert not_supported(httpx.Response(403, content=_status(4, "notSupport", "Invalid Operation", "/x").encode()))


async def test_wrong_temporary_admin_password_is_one_attempt_and_auth_failed() -> None:
    mock = _hik_1970(username="visor", extra_users={"admin": "Adm#Temporal:9"}, write_users={"admin"})
    client = client_for(make_device("hikvision", username="visor"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceAuthFailed) as err:
            await client.set_device_time("admin", "mala")   # type: ignore[attr-defined]
        assert not isinstance(err.value, DeviceWriteDenied)
        assert (await client.device_time()).device_time.year == 1970   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert mock.auth.rejected_credentials == 1 and mock.time_puts == []


async def test_low_privilege_answered_with_200_is_write_denied() -> None:
    """Revisión: un firmware que contesta al PUT del usuario sin permiso con un 200 y `<ResponseStatus>` statusCode 4
    `lowPrivilege` daba un error del equipo (`DeviceProtocolError`) y la web no ofrecía escribir el administrador."""
    mock = _hik_1970()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.method == "PUT":
            return Response(_status(4, "lowPrivilege", "Invalid Operation", request.url.path), 200, media_type=XML)
        return await real_time(request)

    _replace_route(mock.app, "/ISAPI/System/time", time_, ["GET", "PUT"])
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceWriteDenied):
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()


async def test_hikvision_written_but_read_back_failed_is_unverified() -> None:
    """La cámara acepta el PUT y después no contesta a la lectura: la hora SÍ está escrita (no es un fallo de la
    escritura, como se decía antes)."""
    mock = _hik_1970()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.method == "GET" and mock.time_puts:
            return Response("Service Unavailable", 503, media_type="text/plain")
        return await real_time(request)

    _replace_route(mock.app, "/ISAPI/System/time", time_, ["GET", "PUT"])
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceTimeUnverified) as err:
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert err.value.code == "clock_set_unverified" and "503" in err.value.details["reason"]
    assert len(mock.time_puts) == 1 and abs(mock.clock_offset_s) < 2


async def test_put_rejected_by_firmware_is_a_protocol_error() -> None:
    mock = _hik_1970()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.method == "PUT":
            return Response(_status(6, "badXmlContent", "Invalid XML Content", request.url.path), 400, media_type=XML)
        return await real_time(request)

    _replace_route(mock.app, "/ISAPI/System/time", time_, ["GET", "PUT"])
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceProtocolError):
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()


async def test_local_time_without_offset_is_written_the_same_way() -> None:
    """Firmwares que dan `localTime` sin desfase y una zona con horario de verano: se escribe igual, sin desfase y en
    la hora de verano que toque (la regla POSIX del equipo)."""
    from zoneinfo import ZoneInfo
    madrid = "CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00"
    puts: list[str] = []
    state = {"local": datetime(2000, 1, 1, 0, 3, 0)}

    async def time_(request: Request) -> Response:
        if request.method == "PUT":
            body = (await request.body()).decode()
            puts.append(body)
            state["local"] = datetime.fromisoformat(re.search(r"<localTime>([^<]+)<", body).group(1))  # type: ignore[union-attr]
            return Response(_status(1, "ok", "OK", request.url.path), media_type=XML)
        return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="{NS}">\n'
                        f"<timeMode>manual</timeMode>\n<localTime>{state['local'].isoformat()}</localTime>\n"
                        f"<timeZone>{madrid}</timeZone>\n</Time>\n", media_type=XML)

    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera")
    _replace_route(mock.app, "/ISAPI/System/time", time_, ["GET", "PUT"])
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    sent = re.search(r"<localTime>([^<]+)<", puts[0]).group(1)  # type: ignore[union-attr]
    assert "+" not in sent and "Z" not in sent
    expected = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)
    assert abs((datetime.fromisoformat(sent) - expected).total_seconds()) < 2
    assert f"<timeZone>{madrid}</timeZone>" in puts[0] and abs(after.skew_s) < 2


# --------------------------------------------------------------------------- ONVIF
def _onvif_1970(**kw: Any) -> OnvifMock:
    kw.setdefault("password", TEST_PASSWORD)
    kw.setdefault("date_time_type", "Manual")
    return OnvifMock(tz="CST-8:00:00", clock_offset_s=_offset_to(REAL_CAMERA_TIME), **kw)


async def test_onvif_1970_set_keeps_zone_and_signs_with_the_right_clock() -> None:
    """La cámara del usuario sí contesta a ONVIF `GetSystemDateAndTime` (lo midió el revisor). La firma del token
    se ajusta al reloj de 1970 para el Set, y a la hora buena después."""
    mock = _onvif_1970()
    client = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    assert isinstance(client, DeviceClockWriter)
    try:
        after = await client.set_device_time()
        info = await client.probe()   # type: ignore[attr-defined]   # firmado con el reloj nuevo
    finally:
        await client.aclose()
    assert len(mock.set_bodies) == 1 and info.model == mock.model
    body = mock.set_bodies[0]
    assert re.search(r"<\w+:DateTimeType>Manual</", body) and "<tt:TZ>CST-8:00:00</tt:TZ>" in body
    assert re.search(r"<tt:Year>\d{4}</tt:Year>", body) and "<tt:Year>1970" not in body
    assert abs(after.skew_s) < 2 and mock.rejected == 0


async def test_onvif_refuses_ntp_mode() -> None:
    mock = _onvif_1970(date_time_type="NTP")
    client = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceClockManaged):
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert mock.set_bodies == []


async def test_onvif_refuses_when_it_does_not_say_its_time_mode() -> None:
    """Revisión final: sin `DateTimeType` (modo «unknown») se mandaba `SetSystemDateAndTime` con `DateTimeType`
    Manual, que apagaría un NTP que el equipo no dijo tener. CONTRATO §18.3: solo con la hora puesta a mano."""
    mock = _onvif_1970(date_time_type="")
    client = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceClockManaged) as err:
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert mock.set_bodies == [] and err.value.time_mode == "sin informar"


async def test_onvif_not_authorized_on_set_is_write_denied_and_does_not_fuse() -> None:
    """ONVIF Core: un usuario sin la clase WRITE_SYSTEM recibe el mismo `NotAuthorized` que una contraseña mala.
    Como la lectura autenticada de antes pasó, es falta de permisos: ni se funde el cliente ni se dice «contraseña
    incorrecta»."""
    mock = _onvif_1970(can_write=False)
    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceWriteDenied):
            await client.set_device_time()
        assert client._failure is None
        info = await client.probe()
    finally:
        await client.aclose()
    assert info.model == mock.model and len(mock.set_bodies) == 1


async def test_onvif_write_denied_in_http_digest_mode_does_not_fuse() -> None:
    """Equipos ONVIF que autentican por Digest HTTP: la escritura sin permiso llega como 401 con reto, y VendorAuth
    se marca como rechazado. Como la contraseña ya valió en la lectura de antes, se deshace (no queda fundido)."""
    mock = _onvif_1970(can_write=False, http_auth="digest", ws_token=False)
    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceWriteDenied):
            await client.set_device_time()
        assert client._failure is None and client._auth is not None and not client._auth.rejected
        info = await client.probe()
    finally:
        await client.aclose()
    assert info.model == mock.model and len(mock.set_bodies) == 1 and mock.rejected == 0


async def test_onvif_anonymous_device_write_denied_is_not_a_bad_password() -> None:
    """Revisión: un equipo con ONVIF anónimo contesta `GetDeviceInformation` sin credenciales (así se aprende el modo),
    y la contraseña quedaba sin comprobar: el `NotAuthorized` del Set de un usuario sin permiso se tomaba por contraseña
    mala (cliente fundido y, en B6, 30 min sin leer la hora ni sacar instantáneas). Ahora se comprueba antes con
    credenciales."""
    mock = _onvif_1970(anonymous=True, can_write=False)
    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceWriteDenied):
            await client.set_device_time()
        assert client._failure is None
        assert (await client.device_time()).device_time.year == 1970, "el cliente sigue leyendo"
    finally:
        await client.aclose()
    assert len(mock.set_bodies) == 1 and mock.rejected == 0


async def test_onvif_anonymous_device_with_wrong_password_never_reaches_the_set() -> None:
    mock = _onvif_1970(anonymous=True)
    client = client_for(make_device("onvif"), "Mala#000", transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceAuthFailed) as err:
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert not isinstance(err.value, DeviceWriteDenied) and mock.set_bodies == [] and mock.rejected == 1


async def test_onvif_temporary_admin_sets_the_time() -> None:
    """El usuario guardado es de solo lectura y el administrador del equipo se escribe una vez: el Set va firmado
    con él y el usuario guardado no se gasta."""
    mock = _onvif_1970(username="admin", password="Adm#Temporal:9")
    client = client_for(make_device("onvif", username="visor"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time("admin", "Adm#Temporal:9")   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert abs(after.skew_s) < 2 and len(mock.set_bodies) == 1 and mock.rejected == 0
    assert "<wsse:Username>admin</wsse:Username>" in mock.set_bodies[0]


async def test_onvif_ntp_device_with_wrong_password_reports_the_password() -> None:
    """Revisión: con NTP y la contraseña guardada mala, `device_time()` avisaba (`credentials_rejected`), pero se
    lanzaba antes «la hora la pone NTP» y B6 no dejaba la contraseña en pausa: cada clic gastaba otro intento."""
    mock = _onvif_1970(date_time_type="NTP")
    client = client_for(make_device("onvif"), "Mala#000", transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceAuthFailed) as err:
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert not isinstance(err.value, (DeviceClockManaged, DeviceWriteDenied)) and mock.set_bodies == []
    assert mock.rejected == 1


async def test_onvif_1970_reading_shows_the_date_the_camera_shows() -> None:
    """Revisión: «no tiene la hora puesta: marca el …» usaba la fecha UTC; una cámara a 01-01-1970 00:07 en UTC+8
    leída por ONVIF salía como «marca el 31-12-1969». Ahora es la fecha local que da el propio equipo."""
    from vms.ops.health import clock
    mock = _onvif_1970()
    client = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        dt = await client.device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert dt.device_time.utcoffset() == timedelta(hours=8) and dt.skew_s < -1.7e9
    msg = clock.assess_device(dt, 2.0, 30.0, 0, noun="la cámara", pc_serves_time=False).message
    assert "marca el 01-01-1970" in msg and "1969" not in msg, msg
    assert dt.utc_offset_s is None, "el aviso de zona de ONVIF no cambia con esto"


async def test_onvif_bad_password_never_reaches_the_set() -> None:
    mock = _onvif_1970()
    client = client_for(make_device("onvif"), "Mala#000", transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceAuthFailed) as err:
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert not isinstance(err.value, DeviceWriteDenied) and mock.set_bodies == []


async def test_onvif_written_but_read_back_failed_is_unverified() -> None:
    mock = _onvif_1970()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and mock.set_bodies:     # ya escrita: deja de contestar
            await Response("Service Unavailable", 503, media_type="text/plain")(scope, receive, send)
            return
        await mock.app(scope, receive, send)

    client = client_for(make_device("onvif"), TEST_PASSWORD, transport=httpx.ASGITransport(app=app))
    try:
        with pytest.raises(DeviceTimeUnverified):
            await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert len(mock.set_bodies) == 1 and abs(mock.clock_offset_s) < 2


async def test_onvif_device_that_rejects_its_own_zone_is_set_without_it() -> None:
    mock = _onvif_1970(reject_tz=True)
    client = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert len(mock.set_bodies) == 2 and "<tt:TZ>" not in mock.set_bodies[1]
    assert abs(after.skew_s) < 2 and mock.tz == "CST-8:00:00", "sin zona, el equipo se queda con la suya"
