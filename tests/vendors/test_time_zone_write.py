"""«Poner en hora con este PC» con «Usar también la zona horaria de este PC» (CONTRATO §18.3), con los simuladores.

La cámara real del usuario (Hikvision DS-2CD1741FWD-IZ) tiene la zona de fábrica «CST-8:00:00» (China, UTC+8) y el
PC está en Madrid: escribir solo la hora dejaba la hora de la imagen 6 o 7 horas adelantada. Ahora la zona del PC
va en el MISMO PUT que la hora y se comprueba al leer de nuevo. Nunca se habla con una cámara real.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, request_response

from tests.vendors.conftest import TEST_PASSWORD, asgi, make_device
from tools.mocks.hikvision import NS, XML, HikvisionMock, _status
from tools.mocks.onvif import OnvifMock
from vms.core.tzposix import posix_zone, zone_from_iana
from vms.vendors import clock, client_for
from vms.vendors.errors import DeviceTimeUnverified, DeviceZoneNotApplied, DeviceZoneUnsupported
from vms.vendors.hikvision import hik_zone

MADRID = "CET-1CEST,M3.5.0,M10.5.0/3"
HIK_MADRID = "CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00"
SUMMER = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
WINTER = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def at(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Mueve la hora de «este PC» (la que se escribe y la que se mide) a otra fecha, sin tocar el reloj real."""
    def move(when: datetime) -> None:
        shift = when - datetime.now(timezone.utc)
        monkeypatch.setattr(clock, "utc_now", lambda: datetime.now(timezone.utc) + shift)
    yield move


def _china_1970(**kw: Any) -> HikvisionMock:
    """La cámara del usuario: hora a mano, zona de fábrica de China y el reloj en 1970."""
    kw.setdefault("password", TEST_PASSWORD)
    kw.setdefault("time_mode", "manual")
    return HikvisionMock(kind="camera", tz_hours=8, clock_offset_s=-1.79e9, **kw)


def _sent(body: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", body)
    assert m, body
    return m.group(1)


# --------------------------------------------------------------------------- formato de Hikvision
@pytest.mark.parametrize(("name", "expected"), [
    ("Europe/Madrid", HIK_MADRID),
    ("Asia/Shanghai", "CST-8:00:00"),
    ("Asia/Kolkata", "CST-5:30:00"),
    ("Etc/UTC", "CST+0:00:00"),
    ("Europe/London", "CST+0:00:00DST01:00:00,M3.5.0/01:00:00,M10.5.0/02:00:00"),
    ("America/New_York", "CST+5:00:00DST01:00:00,M3.2.0/02:00:00,M11.1.0/02:00:00"),
    ("Australia/Sydney", "CST-10:00:00DST01:00:00,M10.1.0/02:00:00,M4.1.0/03:00:00"),
])
def test_hikvision_zone_format(name: str, expected: str) -> None:
    rule = zone_from_iana(name).rule
    assert hik_zone(rule) == expected
    assert posix_zone(expected) == rule, "se lee de vuelta como la misma zona"


@pytest.mark.parametrize("name", ["America/Santiago", "America/Nuuk", "Asia/Jerusalem", "America/Miquelon"])
def test_zones_hikvision_cannot_express_are_refused(name: str) -> None:
    """Cambios «a las 24:00», «a las −1:00» o «a las 26:00» de tzdata, o un texto que se leería como otra zona: no se
    adivina cómo lo guardaría la cámara."""
    assert hik_zone(zone_from_iana(name).rule) is None


# --------------------------------------------------------------------------- ISAPI
@pytest.mark.parametrize(("when", "offset", "suffix"), [(SUMMER, 7200, "+02:00"), (WINTER, 3600, "+01:00")])
async def test_china_default_gets_madrid_zone_and_time_in_one_put(at: Any, when: datetime, offset: int,
                                                                  suffix: str) -> None:
    at(when)
    mock = _china_1970()
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    assert [r for r in mock.requests if r.startswith("PUT")] == ["PUT /ISAPI/System/time"], "una sola escritura"
    body = mock.time_puts[0]
    assert _sent(body, "timeZone") == HIK_MADRID and _sent(body, "timeMode") == "manual"
    sent = datetime.fromisoformat(_sent(body, "localTime"))
    assert sent.isoformat().endswith(suffix), "la hora ya va en la zona nueva"
    assert abs((sent - when).total_seconds()) < 2
    assert after.zone == HIK_MADRID and after.previous_zone == "CST-8:00:00"
    assert after.utc_offset_s == offset and abs(after.skew_s) < 2


async def test_local_time_without_offset_is_written_in_the_new_zone(at: Any) -> None:
    """Firmwares que dan `localTime` sin desfase: la hora que se escribe es la de Madrid, porque el equipo la va a
    entender en la zona que llega en el mismo PUT."""
    at(SUMMER)
    state: dict[str, Any] = {"local": datetime(1970, 1, 1, 0, 7, 29), "zone": "CST-8:00:00"}
    puts: list[str] = []

    async def time_(request: Request) -> Response:
        if request.method == "PUT":
            body = (await request.body()).decode()
            puts.append(body)
            state["local"] = datetime.fromisoformat(_sent(body, "localTime"))
            state["zone"] = _sent(body, "timeZone")
            return Response(_status(1, "ok", "OK", request.url.path), media_type=XML)
        return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="{NS}">\n'
                        f"<timeMode>manual</timeMode>\n<localTime>{state['local'].isoformat()}</localTime>\n"
                        f"<timeZone>{state['zone']}</timeZone>\n</Time>\n", media_type=XML)

    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera")
    for r in mock.app.inner.routes:
        if isinstance(r, Route) and r.path == "/ISAPI/System/time":
            r.endpoint, r.app, r.methods = time_, request_response(time_), {"GET", "PUT"}
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    sent = _sent(puts[0], "localTime")
    assert "+" not in sent and datetime.fromisoformat(sent) == SUMMER.replace(tzinfo=None) + timedelta(hours=2)
    assert _sent(puts[0], "timeZone") == HIK_MADRID


async def test_zone_without_dst(at: Any) -> None:
    at(SUMMER)
    mock = _china_1970()
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time(zone="IST-5:30")
    finally:
        await client.aclose()
    assert _sent(mock.time_puts[0], "timeZone") == "CST-5:30:00"
    assert _sent(mock.time_puts[0], "localTime").endswith("+05:30") and after.utc_offset_s == 5 * 3600 + 1800


async def test_same_zone_as_the_china_default_is_written_unchanged() -> None:
    """Un PC en China (o en otra zona UTC+8 sin horario de verano): la zona escrita es la misma que ya tenía."""
    mock = _china_1970()
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time(zone="CST-8")
    finally:
        await client.aclose()
    assert _sent(mock.time_puts[0], "timeZone") == "CST-8:00:00"
    assert after.zone == after.previous_zone == "CST-8:00:00" and abs(after.skew_s) < 2


async def test_without_zone_the_device_zone_is_untouched(at: Any) -> None:
    """La casilla sin marcar: como antes, solo cambia la hora (y en la zona del propio equipo)."""
    at(SUMMER)
    mock = _china_1970(tz_text=HIK_MADRID)
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time()
    finally:
        await client.aclose()
    assert _sent(mock.time_puts[0], "timeZone") == HIK_MADRID
    assert _sent(mock.time_puts[0], "localTime").endswith("+02:00")
    assert after.zone == after.previous_zone == HIK_MADRID and after.utc_offset_s == 7200


async def test_zone_ignored_by_the_firmware_is_not_applied() -> None:
    """La cámara contesta OK al PUT pero se queda con su zona: no se da por buena (antes no se miraba la zona)."""
    mock = _china_1970(ignore_zone=True)
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceZoneNotApplied) as err:
            await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    assert len(mock.time_puts) == 1 and err.value.code == "clock_zone_not_applied"
    assert err.value.details == {"wanted": HIK_MADRID, "got": "CST-8:00:00"}
    reading = err.value.reading
    assert reading.zone == reading.previous_zone == "CST-8:00:00" and abs(reading.skew_s) < 2, "la hora sí se escribió"


async def test_zone_hikvision_cannot_express_writes_nothing() -> None:
    mock = _china_1970()
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceZoneUnsupported) as err:
            await client.set_device_time(zone=zone_from_iana("America/Santiago").posix)
    finally:
        await client.aclose()
    assert err.value.code == "clock_zone_unsupported" and mock.time_puts == []


@pytest.mark.parametrize("http_status", [200, 400, 403])
async def test_camera_rejecting_the_pc_zone_is_zone_unsupported(at: Any, http_status: int) -> None:
    """Firmware que rechaza el PUT que lleva la zona nueva (`badParameters` con un 200 o un 400, `notSupport` con un
    403): es lo que no se ha podido probar con la DS-2CD1741FWD-IZ (V5.5.0) del usuario. Antes salía el error genérico
    del equipo (502 `device_protocol_error`, o 501 `device_unsupported`) sin decir que se pulsara sin la casilla, que
    sale marcada justo en ese caso (China → Madrid): cada clic fallaba igual. Ahora es «no admite la zona» (422), no
    ha cambiado nada y sin la zona sí se pone en hora."""
    at(SUMMER)
    mock = _china_1970(reject_zone_http=http_status)
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceZoneUnsupported) as err:
            await client.set_device_time(zone=MADRID)
        assert err.value.code == "clock_zone_unsupported" and err.value.status == 422
        assert err.value.details["zone"] == MADRID and err.value.details["reason"]
        assert len(mock.time_puts) == 1 and _sent(mock.time_puts[0], "timeZone") == HIK_MADRID
        assert mock.tz_text == "" and mock.tz_hours == 8 and mock.clock_offset_s < -1e9, "ni la hora ni la zona"
        after = await client.set_device_time()          # «vuelve a pulsar sin marcar la casilla»
    finally:
        await client.aclose()
    assert _sent(mock.time_puts[1], "timeZone") == "CST-8:00:00"
    assert after.zone == "CST-8:00:00" and abs(after.skew_s) < 2


async def test_written_but_not_read_back_keeps_the_previous_zone() -> None:
    """Hora y zona aceptadas, pero la lectura de comprobación falla: la zona de antes va en el error, para que
    audit.log diga de qué zona se partía (antes quedaba en blanco aunque la zona pudo cambiar)."""
    mock = _china_1970()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.method == "GET" and mock.time_puts:     # ya escrita: deja de contestar a la lectura
            return Response("Service Unavailable", 503, media_type="text/plain")
        return await real_time(request)

    for r in mock.app.inner.routes:
        if isinstance(r, Route) and r.path == "/ISAPI/System/time":
            r.endpoint, r.app = time_, request_response(time_)
    client: Any = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceTimeUnverified) as err:
            await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    assert err.value.details["previous_zone"] == "CST-8:00:00" and mock.tz_text == HIK_MADRID


# --------------------------------------------------------------------------- ONVIF
def _onvif_china(**kw: Any) -> OnvifMock:
    kw.setdefault("password", TEST_PASSWORD)
    kw.setdefault("date_time_type", "Manual")
    return OnvifMock(tz="CST-8:00:00", clock_offset_s=-1.79e9, **kw)


async def test_onvif_writes_the_pc_zone_with_the_time() -> None:
    mock = _onvif_china()
    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    assert len(mock.set_bodies) == 1
    body = mock.set_bodies[0]
    assert f"<tt:TZ>{MADRID}</tt:TZ>" in body and re.search(r"<\w+:DaylightSavings>true</", body)
    expected = zone_from_iana("Europe/Madrid").offset_at(datetime.now(timezone.utc))
    assert after.zone == MADRID and after.previous_zone == "CST-8:00:00" and mock.tz == MADRID
    assert after.device_time.utcoffset() == expected and abs(after.skew_s) < 2


async def test_onvif_without_zone_keeps_its_own() -> None:
    mock = _onvif_china()
    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        after = await client.set_device_time()
    finally:
        await client.aclose()
    assert "<tt:TZ>CST-8:00:00</tt:TZ>" in mock.set_bodies[0] and after.zone == after.previous_zone == "CST-8:00:00"


async def test_onvif_device_rejecting_the_pc_zone_is_not_retried_without_it() -> None:
    """Sin la zona propia, se repetía sin zona (y quedaba la hora puesta con la zona mala); con la del PC, no: se
    dice que no la admite y no ha cambiado nada."""
    mock = _onvif_china(reject_tz=True)
    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceZoneUnsupported):
            await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    assert len(mock.set_bodies) == 1 and mock.tz == "CST-8:00:00" and mock.clock_offset_s < -1e9


async def test_onvif_zone_ignored_by_the_device_is_not_applied() -> None:
    mock = _onvif_china(ignore_tz=True)
    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        with pytest.raises(DeviceZoneNotApplied) as err:
            await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    assert err.value.details["got"] == "CST-8:00:00" and abs(err.value.reading.skew_s) < 2


async def test_onvif_written_but_not_read_back_keeps_the_previous_zone() -> None:
    mock = _onvif_china()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and mock.set_bodies:     # ya escrita: deja de contestar
            await Response("Service Unavailable", 503, media_type="text/plain")(scope, receive, send)
            return
        await mock.app(scope, receive, send)

    client: Any = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(app))
    try:
        with pytest.raises(DeviceTimeUnverified) as err:
            await client.set_device_time(zone=MADRID)
    finally:
        await client.aclose()
    assert err.value.details["previous_zone"] == "CST-8:00:00" and mock.tz == MADRID
