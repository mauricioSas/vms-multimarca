"""«Usar también la zona horaria de este PC» por la API (CONTRATO §18.3), con el driver ISAPI REAL y el simulador.

La cámara del usuario tiene la zona de fábrica «CST-8:00:00» (China, UTC+8) y el PC está en Madrid. «Poner en hora
con este PC» escribía solo la hora, a propósito sin tocar la zona: la hora de la imagen seguía 6 o 7 horas
adelantada y el mensaje decía «cámbiala en la web de la cámara». El «PC» de estas pruebas está siempre en Madrid
(zona y reloj simulados), así el resultado no depende de la zona de la máquina que las pasa.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.ops.conftest import Harness, new_device
from tests.test_spanish_style import voseo_in
from tools.mocks.hikvision import HikvisionMock
from vms.core import tzposix
from vms.core.models import Device
from vms.ops.health import clock as clockmod
from vms.ops.store import OpsStore
from vms.vendors import client_for

PW = "Cl@ve#1"
HIK_MADRID = "CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00"
BOX = "«Usar también la zona horaria de este PC»"
MADRID_TZ = ZoneInfo("Europe/Madrid")


def _madrid_now_s() -> int:
    off = datetime.now(MADRID_TZ).utcoffset()
    assert off is not None
    return int(off.total_seconds())


@pytest.fixture
def pc_in_madrid(monkeypatch: pytest.MonkeyPatch) -> tzposix.PcZone:
    """Este PC en Madrid: su zona (la que se escribe) y su reloj (con el que se compara la zona del equipo)."""
    zone = tzposix.zone_from_iana("Europe/Madrid")
    monkeypatch.setattr(tzposix, "pc_zone", lambda now=None: zone)
    monkeypatch.setattr(clockmod, "_pc_offset_s",
                        lambda instant: int(instant.astimezone(MADRID_TZ).utcoffset().total_seconds()))  # type: ignore[union-attr]
    return zone


def _mock(**kw: Any) -> HikvisionMock:
    kw.setdefault("time_mode", "manual")
    real = datetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=8)))
    return HikvisionMock(password=PW, kind="camera", tz_hours=8,
                         clock_offset_s=(real - datetime.now(timezone.utc)).total_seconds(), **kw)


def _factory(mock: Any) -> Any:
    def f(dev: Device, password: str) -> Any:
        return client_for(dev, password, transport=httpx.ASGITransport(app=mock.app))
    return f


def _audit(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "vms.audit" and '"camera_clock_set"' in r.getMessage()]


async def _camera(api: Harness, mock: HikvisionMock) -> dict[str, Any]:
    admin = await api.login()
    d = await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5", password=PW,
                         import_channels=[1])
    api.state.client_factory = _factory(mock)
    return d


async def test_real_camera_gets_time_and_zone_of_this_pc(api: Harness, pc_in_madrid: tzposix.PcZone,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    mock = _mock()
    d = await _camera(api, mock)
    admin = await api.login()
    before = (await admin.post("/api/clock/check")).json()
    assert before["devices"][0]["utc_offset_s"] == 8 * 3600, "la zona del equipo va en la medida (para el diálogo)"
    assert "UTC+8" in before["devices"][0]["message_es"] and BOX in before["devices"][0]["message_es"]
    view = (await admin.get("/api/clock")).json()
    assert view["pc_zone"] == {"utc_offset_s": _madrid_now_s(), "label_es": "UTC+1 en invierno y UTC+2 en verano",
                               "posix": "CET-1CEST,M3.5.0,M10.5.0/3", "error_es": ""}
    assert view["devices"][0]["utc_offset_s"] == 8 * 3600
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"use_pc_zone": True})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True and res["zone_changed"] is True and abs(res["check"]["skew_s"]) < 2
    assert "ya está en hora" in res["message_es"] and "Ya usa la zona horaria de este PC (UTC+1 en invierno y UTC+2 " \
        "en verano)" in res["message_es"] and "UTC+8" not in res["message_es"]
    assert voseo_in(res["message_es"]) == []
    assert len(mock.time_puts) == 1 and f"<timeZone>{HIK_MADRID}</timeZone>" in mock.time_puts[0]
    after = (await admin.get("/api/clock")).json()["devices"][0]
    assert after["utc_offset_s"] == _madrid_now_s() and "zona horaria" not in after["message_es"]
    line = _audit(caplog)[0]
    assert line["result"] == "ok" and line["use_pc_zone"] is True and line["zone_changed"] is True
    assert line["zone_before"] == "CST-8:00:00" and line["zone_after"] == HIK_MADRID
    assert PW not in json.dumps(line)
    day = datetime.now(timezone.utc).date().isoformat()
    tl = (await admin.get(f"/api/timeline/{d['cameras'][0]}", params={
        "start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z", "layers": "clock"})).json()
    assert any("Hora y zona horaria puestas con las de este PC por admin" in e["title_es"] for e in tl)


async def test_without_the_box_the_zone_is_untouched_and_the_box_is_suggested(
        api: Harness, pc_in_madrid: tzposix.PcZone, caplog: pytest.LogCaptureFixture) -> None:
    mock = _mock()
    d = await _camera(api, mock)
    admin = await api.login()
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True and res["zone_changed"] is False
    assert "<timeZone>CST-8:00:00</timeZone>" in mock.time_puts[0], "la zona del equipo no se toca"
    msg = res["message_es"]
    assert "UTC+8" in msg and BOX in msg and "Qué hacer:" in msg and "este botón no la cambia" not in msg, msg
    line = _audit(caplog)[0]
    assert line["use_pc_zone"] is False and line["zone_changed"] is False
    assert line["zone_before"] == line["zone_after"] == "CST-8:00:00"


async def test_pc_zone_that_cannot_be_written_is_refused_without_contacting_the_camera(
        api: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def no_zone(now: Any = None) -> tzposix.PcZone:
        raise tzposix.PcZoneError("Windows da el cambio de hora de este PC con una fecha fija")
    monkeypatch.setattr(tzposix, "pc_zone", no_zone)
    mock = _mock()
    d = await _camera(api, mock)
    admin = await api.login()
    info = (await admin.get("/api/clock")).json()["pc_zone"]
    assert info["label_es"] == "" and "fecha fija" in info["error_es"] and "Qué hacer:" in info["error_es"]
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"use_pc_zone": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "clock_zone_unsupported", r.text
    msg = r.json()["error"]["message"]
    assert "No se ha cambiado nada" in msg and "Qué hacer:" in msg and BOX in msg and voseo_in(msg) == []
    assert mock.requests == [], "no se habla con la cámara"
    assert [ln["result"] for ln in _audit(caplog)] == ["clock_zone_unsupported"]


async def test_zone_the_camera_cannot_store_writes_nothing(api: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zona de un PC en Chile (cambio de hora «a las 24:00»): el formato de Hikvision no la admite. Ni la hora ni la
    zona se escriben, y se dice qué hacer."""
    santiago = tzposix.zone_from_iana("America/Santiago")
    monkeypatch.setattr(tzposix, "pc_zone", lambda now=None: santiago)
    mock = _mock()
    d = await _camera(api, mock)
    admin = await api.login()
    r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"use_pc_zone": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "clock_zone_unsupported", r.text
    msg = r.json()["error"]["message"]
    assert "no admite la zona horaria de este PC (UTC−4 en invierno y UTC−3 en verano)" in msg and "Qué hacer:" in msg
    assert mock.time_puts == []


@pytest.mark.parametrize("http_status", [200, 400])
async def test_camera_rejecting_the_zone_says_to_press_again_without_the_box(
        api: Harness, pc_in_madrid: tzposix.PcZone, caplog: pytest.LogCaptureFixture, http_status: int) -> None:
    """Firmware que rechaza el PUT con la zona nueva (`badParameters`, con un 200 o un 400). Antes: 502
    `device_protocol_error` «no ha aceptado el cambio de hora… ponla en hora desde la web», sin decir que se pulsara
    sin la casilla, que sale marcada por defecto con la zona de China: el siguiente clic fallaba igual."""
    mock = _mock(reject_zone_http=http_status)
    d = await _camera(api, mock)
    admin = await api.login()
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"use_pc_zone": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "clock_zone_unsupported", r.text
    msg = r.json()["error"]["message"]
    assert "no admite la zona horaria de este PC (UTC+1 en invierno y UTC+2 en verano)" in msg, msg
    assert "No se ha cambiado nada" in msg and f"Qué hacer: vuelve a pulsar «Poner en hora con este PC» sin marcar la " \
        f"casilla {BOX}" in msg and voseo_in(msg) == [] and PW not in msg, msg
    assert len(mock.time_puts) == 1 and mock.tz_text == "" and mock.clock_offset_s < -1e9, "ni la hora ni la zona"
    line = _audit(caplog)[0]
    assert line["result"] == "clock_zone_unsupported" and line["use_pc_zone"] is True and line["zone_changed"] is False
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")          # sin la casilla: sí se pone en hora
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["zone_changed"] is False, r.text


async def test_zone_not_applied_says_so(api: Harness, pc_in_madrid: tzposix.PcZone,
                                        caplog: pytest.LogCaptureFixture) -> None:
    mock = _mock(ignore_zone=True)
    d = await _camera(api, mock)
    admin = await api.login()
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"use_pc_zone": True})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is False and res["zone_changed"] is False and len(mock.time_puts) == 1
    msg = res["message_es"]
    assert "no usa la zona horaria de este PC" in msg and "sigue con UTC+8" in msg and "Qué hacer:" in msg, msg
    assert "La hora sí coincide" in msg
    line = _audit(caplog)[0]
    assert line["result"] == "zone_not_applied" and line["zone_changed"] is False
    assert line["zone_before"] == line["zone_after"] == "CST-8:00:00"


async def test_written_unverified_with_the_zone_records_the_previous_zone(
        api: Harness, pc_in_madrid: tzposix.PcZone, caplog: pytest.LogCaptureFixture) -> None:
    """La cámara acepta la hora y la zona y deja de contestar a la lectura de comprobación: audit.log decía
    `zone_before: null` aunque la zona pudo cambiar. Ahora anota de qué zona se partía."""
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route, request_response
    mock = _mock()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.method == "GET" and mock.time_puts:     # ya escrita: deja de contestar a la lectura
            return Response("Service Unavailable", 503, media_type="text/plain")
        return await real_time(request)
    for r in mock.app.inner.routes:
        if isinstance(r, Route) and r.path == "/ISAPI/System/time":
            r.endpoint, r.app = time_, request_response(time_)
    d = await _camera(api, mock)
    admin = await api.login()
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"use_pc_zone": True})
    assert r.status_code == 200 and r.json()["ok"] is False, r.text
    assert "Se ha escrito la hora y la zona horaria de este PC" in r.json()["message_es"]
    line = _audit(caplog)[0]
    assert line["result"] == "written_unverified" and line["use_pc_zone"] is True
    assert line["zone_before"] == "CST-8:00:00" and line["zone_after"] is None and line["zone_changed"] is False


async def test_pc_zone_is_only_for_administrators(api: Harness, pc_in_madrid: tzposix.PcZone) -> None:
    op = await api.operator()
    assert (await op.get("/api/clock")).json()["pc_zone"] is None


def test_zone_advice_names_the_box_only_with_the_time_set_by_hand() -> None:
    zone = "La cámara usa una zona horaria (UTC+8) distinta de la de este PC (UTC+2): la hora que sale en la imagen " \
           "no es la de la tienda."
    critical = clockmod.assess(-90.0, "manual", 2, 30, zone=zone, can_set=True, noun="la cámara")
    assert critical.action.count("«Poner en hora con este PC»") == 1 and BOX in critical.action
    in_time = clockmod.assess(0.3, "manual", 2, 30, zone=zone, can_set=True, noun="la cámara")
    assert in_time.status == "warning" and in_time.action.startswith("Para corregir la zona") and BOX in in_time.action
    ntp = clockmod.assess(0.3, "ntp", 2, 30, zone=zone, can_set=True, noun="la cámara")
    assert BOX not in ntp.action and "zona horaria en la web de la cámara" in ntp.action
    for a in (critical, in_time, ntp):
        assert a.message.count("Qué hacer:") == 1 and voseo_in(a.message) == []
    assert clockmod.zone_label_es(tzposix.zone_from_iana("Asia/Shanghai").rule) == "UTC+8, sin horario de verano"


def test_row_and_dialog_agree_on_the_zone_of_a_device_without_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión: un equipo con la zona de Madrid y la fecha de 1970, medido en verano. La fila miraba el desfase de su
    propia hora (UTC+1, enero) frente al del PC, que en Windows es el de ahora antes de 1970 (UTC+2): decía que las
    zonas no coincidían y aconsejaba la casilla, mientras el diálogo (la zona del equipo en el instante de la medida)
    decía «ya usa la misma zona» con la casilla sin marcar."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    def windows_pc(instant: datetime) -> int:      # como `_pc_offset_s` en Windows: antes de 1970, la zona de ahora
        moment = instant if instant >= epoch else summer
        return int(moment.astimezone(MADRID_TZ).utcoffset().total_seconds())   # type: ignore[union-attr]
    monkeypatch.setattr(clockmod, "_pc_offset_s", windows_pc)

    def reading(zone: str, hours: int) -> Any:
        from vms.core.interfaces import DeviceTime
        return DeviceTime(device_time=datetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=hours))),
                          measured_at=summer, time_mode="manual", source="isapi", zone=zone, utc_offset_s=hours * 3600)

    madrid = reading(HIK_MADRID, 1)
    assert clockmod.zone_mismatch(madrid, noun="la cámara") is None
    chk = clockmod.device_check("dev-1", None, madrid, 2, 30, noun="la cámara", pc_serves_time=False)
    assert chk.utc_offset_s == 7200 and "zona horaria" not in chk.message_es, chk.message_es
    china = clockmod.device_check("dev-1", None, reading("CST-8:00:00", 8), 2, 30, noun="la cámara",
                                  pc_serves_time=False)
    assert china.utc_offset_s == 8 * 3600 and "UTC+8" in china.message_es and "(UTC+2)" in china.message_es
    assert BOX in china.message_es


def test_old_database_gets_the_zone_column(tmp_path: Path) -> None:
    """Una base de una versión anterior (sin `utc_offset_s`) se abre sin perder las medidas."""
    path = tmp_path / "ops.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript("CREATE TABLE clock_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT, "
                       "device_id TEXT, at TEXT NOT NULL, skew_s REAL, round_trip_ms REAL, time_mode TEXT NOT NULL "
                       "DEFAULT 'unknown', status TEXT NOT NULL DEFAULT 'unknown', message_es TEXT NOT NULL DEFAULT '');"
                       "INSERT INTO clock_checks (device_id, at, skew_s, status) VALUES "
                       "('dev-00000001', '2026-10-08T10:00:00.000000+00:00', 1.5, 'ok');")
    conn.close()
    store = OpsStore(path)
    try:
        old = store.latest_clock_checks()["dev-00000001"]
        assert old.skew_s == 1.5 and old.utc_offset_s is None
        from vms.ops.models import ClockCheck
        store.add_clock_check(ClockCheck(device_id="dev-00000001", skew_s=0.1, status="ok", utc_offset_s=28800))
        assert store.latest_clock_checks()["dev-00000001"].utc_offset_s == 28800
    finally:
        store.close()
    OpsStore(path).close()     # abrirla otra vez no intenta añadir la columna de nuevo
