"""Regresiones de la revisión cruzada de la v2 (docs/revision-v2/operacion.md): drivers de Hikvision, Dahua y ONVIF.

Cada prueba recrea la condición del fallo que encontró el revisor y afirma lo correcto.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route, request_response

from tests.vendors.conftest import TEST_PASSWORD, asgi, make_device
from tools.mocks.dahua import DahuaMock
from tools.mocks.hikvision import NS, XML, HikvisionMock, _status
from tools.mocks.onvif import OnvifMock
from vms.ops.health.clock import device_check
from vms.vendors import client_for
from vms.vendors.errors import BasicNotAllowed

PC_MADRID_SUMMER = 2 * 3600


def _replace_route(app: Any, path: str, endpoint: Any) -> None:
    for r in app.inner.routes:
        if isinstance(r, Route) and r.path == path:
            r.endpoint, r.app = endpoint, request_response(endpoint)
            return
    raise AssertionError(f"ruta {path} no encontrada en el simulador")


async def _time(mock: Any, vendor: str, password: str = TEST_PASSWORD) -> Any:
    c = client_for(make_device(vendor), password, transport=asgi(mock.app))
    try:
        return await c.device_time()
    finally:
        await c.aclose()


# --------------------------------------------------------------------------- M2: misma semántica en todas las marcas
async def test_wrong_factory_zone_same_verdict_for_hikvision_and_dahua() -> None:
    """Zona de fábrica UTC+8 con NTP bien: el reloj está en hora (desfase ≈ 0 en UTC) en las dos marcas, y en las
    dos sale el MISMO aviso aparte: la hora sobreimpresa no es la de la tienda. Antes: Hikvision «ok» y Dahua
    «crítico, 8 h adelantada»."""
    hik = HikvisionMock(password=TEST_PASSWORD, kind="camera")

    async def hik_time(request: Request) -> Response:
        local = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="{NS}">\n<timeMode>NTP</timeMode>\n'
                f"<localTime>{local.isoformat(timespec='seconds')}</localTime>\n<timeZone>CST-8:00:00</timeZone>\n"
                "</Time>\n")
        return Response(body, media_type=XML)

    _replace_route(hik.app, "/ISAPI/System/time", hik_time)
    dah = DahuaMock(password=TEST_PASSWORD, kind="camera", tz_hours=8)
    verdicts = {}
    for vendor, mock in (("hikvision", hik), ("dahua", dah)):
        t = await _time(mock, vendor)
        assert abs(t.skew_s) < 3, (vendor, t.skew_s)
        assert t.utc_offset_s == 8 * 3600, vendor
        chk = device_check("dev-00000001", None, t, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER)
        verdicts[vendor] = chk.status
        assert "otra zona horaria (UTC+8)" in chk.message_es and "UTC+2" in chk.message_es
    assert verdicts == {"hikvision": "warning", "dahua": "warning"}


async def test_same_zone_as_pc_is_ok() -> None:
    t = await _time(DahuaMock(password=TEST_PASSWORD, kind="camera", tz_hours=2), "dahua")
    chk = device_check("dev-00000001", None, t, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER)
    assert chk.status == "ok" and "zona" not in chk.message_es


async def test_dahua_clock_really_wrong_is_still_critical() -> None:
    t = await _time(DahuaMock(password=TEST_PASSWORD, kind="camera", tz_hours=2, clock_offset_s=600), "dahua")
    chk = device_check("dev-00000001", None, t, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER)
    assert chk.status == "critical" and abs(chk.skew_s - 600) < 3


# --------------------------------------------------------------------------- B1: fecha de Dahua sin ceros
async def test_dahua_date_without_leading_zeros() -> None:
    dah = DahuaMock(password=TEST_PASSWORD, kind="camera")

    async def current_time(request: Request) -> Response:
        n = datetime.now()
        return PlainTextResponse(f"result={n.year}-{n.month}-{n.day} {n.hour}:{n.minute:02d}:{n.second:02d}\r\n")

    _replace_route(dah.app, "/cgi-bin/global.cgi", current_time)
    t = await _time(dah, "dahua")
    assert abs(t.skew_s) < 3


# --------------------------------------------------------------------------- B2: horario de verano POSIX
async def test_hikvision_local_time_without_offset_uses_dst_rule() -> None:
    hik = HikvisionMock(password=TEST_PASSWORD, kind="camera")

    async def hik_time(request: Request) -> Response:
        local = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Time version="2.0" xmlns="{NS}">\n<timeMode>NTP</timeMode>\n'
                f"<localTime>{local.isoformat(timespec='seconds')}</localTime>\n"
                "<timeZone>CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00</timeZone>\n</Time>\n")
        return Response(body, media_type=XML)

    _replace_route(hik.app, "/ISAPI/System/time", hik_time)
    t = await _time(hik, "hikvision")
    assert abs(t.skew_s) < 3   # antes: 3600 s falsos en verano


# --------------------------------------------------------------------------- B3: modos de hora que no son «manual»
@pytest.mark.parametrize("mode", ["satellite", "timecorrect", "SDK", "ONVIF"])
async def test_hikvision_time_modes_that_are_not_manual(mode: str) -> None:
    t = await _time(HikvisionMock(password=TEST_PASSWORD, kind="camera", time_mode=mode), "hikvision")
    assert t.time_mode == "unknown"
    assert device_check("dev-00000001", None, t, 2.0, 30.0).status == "ok"


# --------------------------------------------------------------------------- M1: 403 notSupport en un recurso
async def test_hikvision_403_not_support_does_not_abort_security_settings() -> None:
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera")
    orig = mock.network_flag

    async def network_flag(request: Request) -> Response:
        if request.path_params["what"] == "telnetd":
            return Response(_status(4, "notSupport", "Invalid Operation", request.url.path), 403, media_type=XML)
        return await orig(request)

    _replace_route(mock.app, "/ISAPI/System/Network/{what}", network_flag)
    c = client_for(make_device("hikvision", username="visor"), "x", transport=asgi(mock.app))
    try:
        s = await c.security_settings("admin", TEST_PASSWORD)
    finally:
        await c.aclose()
    assert s.telnet_enabled is None and s.upnp_enabled is True and s.ssh_enabled is False


# --------------------------------------------------------------------------- M5: la contraseña de administrador nunca en Basic
class _Spy(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner, self.basic = inner, []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        a = request.headers.get("authorization", "")
        if a.startswith("Basic "):
            self.basic.append(base64.b64decode(a.split()[1]).decode())
        return await self.inner.handle_async_request(request)


@pytest.mark.parametrize("vendor", ["hikvision", "dahua"])
async def test_admin_password_never_travels_in_basic(vendor: str) -> None:
    mock: Any = (HikvisionMock(password="AdminTemporal#1", kind="camera", auth_mode="basic") if vendor == "hikvision"
                 else DahuaMock(password="AdminTemporal#1", kind="camera", auth_mode="basic"))
    spy = _Spy(asgi(mock.app))
    c = client_for(make_device(vendor, username="visor", allow_basic=True, https=False), "visor-pass", transport=spy)
    try:
        with pytest.raises(BasicNotAllowed):
            await c.security_settings("admin", "AdminTemporal#1")
    finally:
        await c.aclose()
    assert not any("AdminTemporal#1" in b for b in spy.basic)


# --------------------------------------------------------------------------- M4: ONVIF con la contraseña mala
async def test_onvif_time_survives_rejected_password_and_says_so() -> None:
    """La hora se lee sin autenticar; `GetNTP` rechaza la contraseña. Antes se tiraba la hora y cada comprobación
    horaria gastaba un intento (bloqueo del usuario en el equipo)."""
    mock = OnvifMock(password="Buena#1234", clock_offset_s=600)
    t = await _time(mock, "onvif", password="Mala#0000")
    assert abs(t.skew_s - 600) < 3 and t.credentials_rejected and t.ntp_server == ""
    good = await _time(OnvifMock(password="Buena#1234"), "onvif", password="Buena#1234")
    assert not good.credentials_rejected
