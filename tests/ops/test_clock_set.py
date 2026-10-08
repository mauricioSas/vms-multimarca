"""«Poner en hora con este PC» por la API y relectura de la hora al volver el vídeo (CONTRATO §18.3).

Driver ISAPI REAL contra el simulador de Hikvision con la lectura real de la cámara del usuario (01-01-1970 00:07:29
en UTC+8, hora puesta a mano). Antes no había ninguna ruta ni botón para escribir la hora: la única «solución» que
daba el programa era «Activa NTP en el equipo apuntando a este PC», que en ese PC no sirve la hora.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from tests.ops.conftest import Harness, new_device, wait_for
from tools.mocks.hikvision import HikvisionMock
from vms.core.models import Device
from vms.vendors import client_for

PW = "Cl@ve#1"
ADMIN_PW = "Adm#Temporal:9"
REAL_CAMERA_TIME = datetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=8)))


def _mock(**kw: Any) -> HikvisionMock:
    kw.setdefault("time_mode", "manual")
    return HikvisionMock(password=PW, kind="camera", tz_hours=8,
                         clock_offset_s=(REAL_CAMERA_TIME - datetime.now(timezone.utc)).total_seconds(), **kw)


def _factory(mock: Any) -> Any:
    """Driver real contra el simulador (`HikvisionMock` u `OnvifMock`)."""
    def f(dev: Device, password: str) -> Any:
        return client_for(dev, password, transport=httpx.ASGITransport(app=mock.app))
    return f


def _audit(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "vms.audit" and f'"{event}"' in r.getMessage()]


async def _camera(api: Harness, **over: Any) -> dict[str, Any]:
    admin = await api.login()
    return await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5",
                            password=PW, import_channels=[1], **over)


async def test_set_clock_of_a_1970_camera_from_the_api(api: Harness, caplog: pytest.LogCaptureFixture) -> None:
    mock = _mock()
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    op = await api.operator()
    assert (await op.post(f"/api/clock/devices/{d['id']}/set")).status_code == 403
    assert mock.time_puts == [], "un operador no puede escribir la hora"

    before = (await admin.post("/api/clock/check")).json()["devices"][0]
    assert before["status"] == "critical" and "01-01-1970" in before["message_es"]
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True and abs(res["check"]["skew_s"]) < 2 and res["admin_credentials_used"] is False
    assert "ya está en hora" in res["message_es"] and "UTC+8" in res["message_es"] and "Qué hacer:" in res["message_es"]
    assert len(mock.time_puts) == 1 and "<timeZone>CST-8:00:00</timeZone>" in mock.time_puts[0]
    latest = (await admin.get("/api/clock")).json()["devices"][0]
    assert abs(latest["skew_s"]) < 2 and latest["status"] == "warning", "queda el aviso de la zona (UTC+8)"
    lines = _audit(caplog, "camera_clock_set")
    assert len(lines) == 1 and lines[0]["result"] == "ok" and lines[0]["user"] == "admin"
    assert lines[0]["skew_before_s"] < -1.7e9 and abs(lines[0]["skew_after_s"]) < 2
    cam = d["cameras"][0]
    day = datetime.now(timezone.utc).date().isoformat()
    tl = (await admin.get(f"/api/timeline/{cam}", params={"start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z",
                                                          "layers": "clock"})).json()
    assert any("Hora puesta con la de este PC por admin" in e["title_es"] for e in tl)


async def test_read_only_user_asks_for_admin_and_never_blocks_the_reads(api: Harness,
                                                                        caplog: pytest.LogCaptureFixture) -> None:
    """El usuario guardado es de solo lectura (lo recomendado): 403 `device_write_denied` con qué hacer, SIN dejar
    de leer la hora (antes el 403 de un firmware se tomaba por contraseña mala y bloqueaba 30 min las lecturas).
    Con el administrador temporal se pone en hora y la contraseña no aparece en ningún registro."""
    mock = _mock(username="admin", extra_users={"admin-hora": ADMIN_PW}, write_users={"admin-hora"})
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    with caplog.at_level(logging.DEBUG):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set")
        assert r.status_code == 403, r.text
        err = r.json()["error"]
        assert err["code"] == "device_write_denied"
        assert "solo lectura" in err["message"] and "Qué hacer:" in err["message"]
        chk = (await admin.post("/api/clock/check")).json()["devices"][0]
        assert chk["status"] == "critical" and "rechazó la contraseña" not in chk["message_es"]
        r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"username": "admin-hora", "password": ADMIN_PW})
        assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["admin_credentials_used"] is True
    assert ADMIN_PW not in caplog.text and PW not in caplog.text
    lines = _audit(caplog, "camera_clock_set")
    assert [ln["result"] for ln in lines] == ["device_write_denied", "ok"]
    assert lines[1]["admin_credentials_used"] is True
    assert "admin-hora" not in json.dumps(lines), "CONTRATO §18.3: ni el usuario administrador en audit.log"


async def test_read_only_user_rejected_like_a_bad_password_is_not_retried(api: Harness,
                                                                          caplog: pytest.LogCaptureFixture) -> None:
    """Revisión: un firmware que contesta con 401 al PUT del usuario de solo lectura puede contarlo como intento
    fallido. Cada clic sin escribir el administrador gastaba un intento: al tercero, usuario bloqueado (y sin vídeo,
    que usa ese mismo usuario). Ahora se dice cuántos le quedan y no se vuelve a probar con el usuario guardado."""
    mock = _mock(username="vms", extra_users={"admin": ADMIN_PW}, write_users={"admin"}, write_denied_status=401,
                 lock_after=3)
    admin = await api.login()
    d = await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5",
                         username="vms", password=PW, import_channels=[1])
    api.state.client_factory = _factory(mock)
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set")
        assert r.status_code == 403 and r.json()["error"]["code"] == "device_write_denied", r.text
        msg = r.json()["error"]["message"]
        assert "solo lectura" in msg and "le quedan 2 intentos" in msg and "Qué hacer:" in msg, msg
        for _ in range(3):
            r = await admin.post(f"/api/clock/devices/{d['id']}/set")
            assert r.status_code == 403 and r.json()["error"]["code"] == "device_write_denied", r.text
            assert "no se vuelve a probar" in r.json()["error"]["message"]
    assert len(mock.time_puts) == 1 and mock.auth.rejected_credentials == 1 and not mock.locked
    assert [ln["result"] for ln in _audit(caplog, "camera_clock_set")] == ["device_write_denied"] * 4
    chk = (await admin.post("/api/clock/check")).json()["devices"][0]
    assert chk["status"] == "critical", "la hora se sigue leyendo con el usuario guardado"
    r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    ops = api.app.state.ops
    assert ops._stored_cannot_write(d["id"], PW), "el usuario guardado sigue sin poder"
    assert not ops._stored_cannot_write(d["id"], "Otra#clave"), "con otra contraseña guardada se vuelve a probar"


async def test_wrong_stored_password_is_tried_once_and_admin_typo_is_not_blocked(api: Harness) -> None:
    """La contraseña guardada mala se prueba UNA vez (el siguiente clic no gasta otro intento: bloquearía el usuario de
    la cámara); unas credenciales de administrador mal escritas no bloquean nada y se pueden corregir."""
    mock = _mock()
    admin = await api.login()
    d = await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5",
                         password="Mala#000", import_channels=[1])
    api.state.client_factory = _factory(mock)
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 502 and r.json()["error"]["code"] == "device_auth_failed"
    assert "contraseña guardada" in r.json()["error"]["message"] and mock.auth.rejected_credentials == 1
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 502 and "rechazó hace poco" in r.json()["error"]["message"]
    assert mock.auth.rejected_credentials == 1, "ni un intento más con la contraseña guardada"
    r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"username": "admin", "password": "Otra#mala"})
    assert r.status_code == 502 and "administrador" in r.json()["error"]["message"]
    r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"username": "admin", "password": PW})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert mock.auth.rejected_credentials == 2 and len(mock.time_puts) == 1


async def test_refusals_before_contacting_the_device_are_audited(api: Harness,
                                                                 caplog: pytest.LogCaptureFixture) -> None:
    """Revisión: las negativas sin hablar con el equipo (marca sin `time_write`, contraseña en pausa) no quedaban en
    audit.log; solo los intentos que llegaban al driver."""
    mock = _mock()
    admin = await api.login()
    hik = await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5",
                           password="Mala#000", import_channels=[1])
    dahua = await new_device(admin, name="Grabador", vendor="dahua", kind="nvr", host="10.0.0.6", password=PW,
                             import_channels=[1])
    api.state.client_factory = _factory(mock)
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{dahua['id']}/set")
        assert r.status_code == 422 and r.json()["error"]["code"] == "clock_set_unsupported"
        assert (await admin.post(f"/api/clock/devices/{hik['id']}/set")).status_code == 502   # la prueba con el equipo
        assert (await admin.post(f"/api/clock/devices/{hik['id']}/set")).status_code == 502   # en pausa: sin probar
    lines = _audit(caplog, "camera_clock_set")
    assert [(ln["device_id"], ln["result"]) for ln in lines] == [
        (dahua["id"], "clock_set_unsupported"), (hik["id"], "device_auth_failed"), (hik["id"], "device_auth_failed")]
    assert mock.auth.rejected_credentials == 1 and "Mala#000" not in caplog.text


async def test_half_typed_admin_credentials_are_refused_not_dropped(api: Harness) -> None:
    """Revisión: con solo la contraseña (o un usuario de espacios) se descartaba lo escrito sin decir nada y se probaba
    con el usuario guardado, el de solo lectura: volvía «es de solo lectura» y otro intento contra el equipo."""
    mock = _mock()
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    for body in ({"password": ADMIN_PW}, {"username": "   ", "password": ADMIN_PW}, {"username": "admin"},
                 {"username": "admin", "password": ""}):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set", json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", (body, r.text)
        assert "Qué hacer:" in r.json()["error"]["message"]
    assert mock.requests == [], "no se habla con el equipo"


async def test_second_click_while_setting_is_a_conflict(api: Harness, caplog: pytest.LogCaptureFixture) -> None:
    """Dos clics seguidos (o dos administradores a la vez): el segundo no escribe otra vez; 409 con qué hacer y
    anotado en audit.log."""
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route, request_response
    mock = _mock()
    release = asyncio.Event()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.method == "PUT":
            await release.wait()          # la cámara tarda en contestar a la escritura
        return await real_time(request)
    for r in mock.app.inner.routes:
        if isinstance(r, Route) and r.path == "/ISAPI/System/time":
            r.endpoint, r.app = time_, request_response(time_)
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()

    async def reading() -> bool:
        return "GET /ISAPI/System/time" in mock.requests
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        first = asyncio.create_task(admin.post(f"/api/clock/devices/{d['id']}/set"))
        await wait_for(reading)
        second = await admin.post(f"/api/clock/devices/{d['id']}/set")
        release.set()
        done = await first
    assert second.status_code == 409 and second.json()["error"]["code"] == "conflict", second.text
    assert "Qué hacer:" in second.json()["error"]["message"]
    assert done.status_code == 200 and done.json()["ok"] is True, done.text
    assert len(mock.time_puts) == 1
    assert [ln["result"] for ln in _audit(caplog, "camera_clock_set")] == ["conflict", "ok"]


async def test_admin_password_has_the_same_length_cap_as_other_credentials(api: Harness) -> None:
    mock = _mock()
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    r = await admin.post(f"/api/clock/devices/{d['id']}/set", json={"username": "admin", "password": "x" * 257})
    assert r.status_code == 422 and mock.requests == []


async def test_locked_user_is_not_tried_again(api: Harness) -> None:
    mock = _mock(lock_after=1)
    admin = await api.login()
    d = await new_device(admin, name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5",
                         password="Mala#000", import_channels=[1])
    api.state.client_factory = _factory(mock)
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 502 and r.json()["error"]["code"] == "device_locked", r.text
    assert "bloqueado" in r.json()["error"]["message"] and "Qué hacer:" in r.json()["error"]["message"]
    sent = mock.auth.credentialed
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 502 and mock.auth.credentialed == sent, "con el usuario bloqueado no se insiste"


async def test_restricted_user_on_the_time_read_does_not_pause_the_device(api: Harness) -> None:
    """Revisión: un usuario sin permiso para leer la hora (403 `lowPrivilege` en el GET) se tomaba por contraseña
    mala: la API del equipo quedaba en pausa 30 min (instantáneas incluidas) y se decía «rechazó la contraseña»."""
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route, request_response

    from tools.mocks.hikvision import XML, _status
    mock = _mock()
    real_time = mock.time

    async def time_(request: Request) -> Response:
        if request.method == "GET":
            return Response(_status(4, "lowPrivilege", "Invalid Operation", request.url.path), 403, media_type=XML)
        return await real_time(request)
    for r in mock.app.inner.routes:
        if isinstance(r, Route) and r.path == "/ISAPI/System/time":
            r.endpoint, r.app = time_, request_response(time_)
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    first = (await admin.post("/api/clock/check")).json()["devices"][0]
    sent = mock.requests.count("GET /ISAPI/System/time")
    second = (await admin.post("/api/clock/check")).json()["devices"][0]
    assert first["status"] == second["status"] == "unknown"
    assert "contraseña" not in second["message_es"] and "permiso" in second["message_es"], second
    assert "Qué hacer:" in second["message_es"], "revisión: el texto nuevo de la tabla no decía qué hacer"
    assert mock.requests.count("GET /ISAPI/System/time") > sent, "la segunda lectura sí llega al equipo"
    assert not api.app.state.ops.auth_backoff.blocked(d["id"], PW)


async def test_onvif_anonymous_camera_write_denied_keeps_reading(api: Harness) -> None:
    """Revisión: cámara ONVIF que contesta `GetDeviceInformation` sin credenciales y usuario guardado sin permiso para
    cambiar la hora: se decía «contraseña incorrecta» y la API del equipo quedaba 30 min en pausa (sin leer la hora ni
    sacar instantáneas)."""
    from tools.mocks.onvif import OnvifMock
    mock = OnvifMock(password=PW, anonymous=True, can_write=False, date_time_type="Manual", tz="CST-8:00:00",
                     clock_offset_s=(REAL_CAMERA_TIME - datetime.now(timezone.utc)).total_seconds())
    admin = await api.login()
    d = await new_device(admin, name="Cámara caja", vendor="onvif", kind="camera", host="10.0.0.5", password=PW,
                         import_channels=[1])
    api.state.client_factory = _factory(mock)
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 403 and r.json()["error"]["code"] == "device_write_denied", r.text
    assert not api.app.state.ops.auth_backoff.blocked(d["id"], PW)
    chk = (await admin.post("/api/clock/check")).json()["devices"][0]
    assert chk["status"] == "critical" and "contraseña" not in chk["message_es"], chk


async def test_time_written_but_not_read_back_says_so(api: Harness, caplog: pytest.LogCaptureFixture) -> None:
    """Revisión: si la cámara acepta la hora y justo después no contesta a la lectura de comprobación, se decía que no
    se había podido poner en hora (y audit.log anotaba el fallo de la lectura), pero la hora sí estaba escrita."""
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
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    before = (await admin.post("/api/clock/check")).json()["devices"][0]
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is False and len(mock.time_puts) == 1
    assert "Se ha escrito la hora de este PC en la cámara" in res["message_es"], res
    assert "Qué hacer:" in res["message_es"] and "Comprobar ahora" in res["message_es"]
    assert [ln["result"] for ln in _audit(caplog, "camera_clock_set")] == ["written_unverified"]
    latest = (await admin.get("/api/clock")).json()["devices"][0]
    assert latest["at"] == before["at"], "no se guarda una medida que no se hizo"


async def test_hikcentral_owned_clock_is_refused_without_writing(api: Harness) -> None:
    mock = _mock(time_mode="SDK")
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    admin = await api.login()
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 409, r.text
    msg = r.json()["error"]["message"]
    assert r.json()["error"]["code"] == "device_clock_managed" and "HikCentral" in msg and "Qué hacer:" in msg
    assert mock.time_puts == []


async def test_unreachable_camera_says_what_to_do(api: Harness) -> None:
    d = await _camera(api)

    def factory(dev: Device, password: str) -> Any:
        return client_for(dev.model_copy(update={"host": "127.0.0.1", "http_port": 9}), password, timeout=0.5)
    api.state.client_factory = factory
    admin = await api.login()
    r = await admin.post(f"/api/clock/devices/{d['id']}/set")
    assert r.status_code == 502 and r.json()["error"]["code"] == "device_unreachable"
    assert "no responde" in r.json()["error"]["message"] and "Qué hacer:" in r.json()["error"]["message"]


async def test_clock_is_reread_as_soon_as_the_video_comes_back(api: Harness) -> None:
    """Corte de luz corto: la cámara vuelve con 01-01-1970. Antes solo se miraba en la comprobación horaria (hasta
    1 h con la fecha falsa en la imagen) y nada miraba la hora al volver el vídeo. Nunca se escribe sola."""
    mock = _mock()
    d = await _camera(api)
    cam = d["cameras"][0]
    api.state.client_factory = _factory(mock)
    ops = api.app.state.ops
    ops._clock_first_pending = False              # la primera comprobación horaria tras arrancar ya pasó
    online = {"value": False}

    async def overview() -> dict[str, Any]:
        return {"status": "ok", "cameras": [{"camera_id": cam, "enabled": True, "online": online["value"],
                                             "last_error": ""}]}
    api.state.overview = overview   # type: ignore[method-assign]
    await ops._check_down()                       # un sondeo sin vídeo (aún sin aviso de «cámara sin vídeo»)
    assert ops.store.latest_clock_checks().get(d["id"]) is None
    online["value"] = True
    await ops._check_down()                       # vuelve el vídeo
    for task in list(ops._background):
        await task
    chk = ops.store.latest_clock_checks().get(d["id"])
    assert chk is not None and chk.status == "critical" and "no tiene la hora puesta" in chk.message_es
    assert mock.time_puts == [], "la hora solo se escribe cuando lo pide un administrador"
    online["value"] = False
    await ops._check_down()
    online["value"] = True
    await ops._check_down()                       # otra vuelta en menos de un minuto: no se relee otra vez
    assert not ops._background


async def test_no_extra_clock_read_while_the_first_hourly_check_is_due(api: Harness,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión: al arrancar, la vigilancia (primer sondeo a los 30 s) ve la cámara caída mientras el motor la conecta
    y de vuelta a los 60 s, justo cuando llega la primera comprobación horaria: dos lecturas seguidas del equipo y,
    con la hora mal, dos avisos graves y dos eventos. Hasta esa primera comprobación, que lee todos los equipos, no
    se relee al volver el vídeo; después, sí."""
    mock = _mock()
    d = await _camera(api)
    cam = d["cameras"][0]
    api.state.client_factory = _factory(mock)
    ops = api.app.state.ops                       # recién arrancado: su bucle de la hora aún no ha leído nada
    emitted: list[Any] = []

    async def emit(alert: Any) -> None:
        emitted.append(alert)
    monkeypatch.setattr(ops.notifier, "emit", emit)
    online = {"value": False}

    async def overview() -> dict[str, Any]:
        return {"status": "ok", "cameras": [{"camera_id": cam, "enabled": True, "online": online["value"],
                                             "last_error": ""}]}
    api.state.overview = overview   # type: ignore[method-assign]
    await ops._check_down()                       # el motor aún está conectando la cámara
    online["value"] = True
    await ops._check_down()                       # ya da vídeo
    assert not ops._background and "GET /ISAPI/System/time" not in mock.requests

    ops.initial_delay = -15.0                     # el bucle de la hora de verdad, sin esperar sus 60 s
    loop_task = asyncio.create_task(ops._clock_loop())

    async def first_done() -> bool:
        return not ops._clock_first_pending
    try:
        await wait_for(first_done)
    finally:
        loop_task.cancel()
    first = mock.requests.count("GET /ISAPI/System/time")
    assert first > 0 and [a.kind for a in emitted if a.kind == "clock_skew"] == ["clock_skew"]
    online["value"] = False
    await ops._check_down()
    online["value"] = True
    await ops._check_down()                       # después de la primera comprobación, sí se relee
    for task in list(ops._background):
        await task
    assert mock.requests.count("GET /ISAPI/System/time") > first


async def _flap(api: Harness, cam: str, times: int, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """La cámara pierde el vídeo un sondeo y vuelve, `times` veces, cada una más de un minuto después de la anterior.
    Devuelve los avisos emitidos."""
    from types import SimpleNamespace

    from vms.ops import service as service_mod
    ops = api.app.state.ops
    ops._clock_first_pending = False              # la primera comprobación horaria tras arrancar ya pasó
    emitted: list[Any] = []

    async def emit(alert: Any) -> None:
        emitted.append(alert)
    monkeypatch.setattr(ops.notifier, "emit", emit)
    clock = {"now": 1000.0}
    # solo el reloj del servicio (el del bucle de asyncio sigue siendo el real)
    monkeypatch.setattr(service_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    online = {"value": True}

    async def overview() -> dict[str, Any]:
        return {"status": "ok", "cameras": [{"camera_id": cam, "enabled": True, "online": online["value"],
                                             "last_error": ""}]}
    api.state.overview = overview   # type: ignore[method-assign]
    for _ in range(times):
        online["value"] = False
        await ops._check_down()
        online["value"] = True
        await ops._check_down()
        for task in list(ops._background):
            await task
        clock["now"] += service_mod.CLOCK_RECHECK_MIN_S + 61
    return emitted


async def test_flapping_camera_with_a_bad_clock_alerts_once(api: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión: una cámara a 1970 que pierde el vídeo un sondeo cada 2 minutos (enlace inestable, PoE, recarga de la
    ruta en MediaMTX) daba un aviso GRAVE «Hora desajustada» en cada vuelta (5 en 10 minutos; los graves se saltan
    las horas de silencio). Ahora la relectura solo avisa si la hora empeora; la medida se sigue guardando."""
    mock = _mock()
    d = await _camera(api)
    api.state.client_factory = _factory(mock)
    emitted = await _flap(api, d["cameras"][0], 5, monkeypatch)
    assert [a.kind for a in emitted if a.kind == "clock_skew"] == ["clock_skew"]
    assert mock.requests.count("GET /ISAPI/System/time") >= 5, "se relee en cada vuelta"
    admin = await api.login()
    day = datetime.now(timezone.utc).date().isoformat()
    tl = (await admin.get(f"/api/timeline/{d['cameras'][0]}", params={
        "start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z", "layers": "clock"})).json()
    assert len(tl) == 1, tl
    chk = api.app.state.ops.store.latest_clock_checks().get(d["id"])
    assert chk is not None and chk.status == "critical"


async def test_ntp_camera_gets_its_time_before_the_reread_alerts(api: Harness,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión: una cámara con NTP vuelve de un corte de luz con 01-01-1970 y tarda un poco en sincronizar; leerla
    en el mismo segundo daba un aviso grave falso («el servidor NTP no se la da bien») hasta la hora siguiente. Se
    vuelve a leer pasado un margen antes de avisar."""
    from vms.ops import service as service_mod
    monkeypatch.setattr(service_mod, "CLOCK_CONFIRM_S", 0.0)
    mock = HikvisionMock(password=PW, kind="camera", time_mode="NTP",
                         clock_offset_s=(REAL_CAMERA_TIME - datetime.now(timezone.utc)).total_seconds())
    d = await _camera(api)
    reads = {"n": 0}

    def factory(dev: Device, password: str) -> Any:
        reads["n"] += 1
        if reads["n"] == 2:
            mock.clock_offset_s = 0.0      # el servidor NTP ya le ha dado la hora
        return client_for(dev, password, transport=httpx.ASGITransport(app=mock.app))
    api.state.client_factory = factory
    emitted = await _flap(api, d["cameras"][0], 1, monkeypatch)
    assert reads["n"] == 2 and [a for a in emitted if a.kind == "clock_skew"] == []
    chk = api.app.state.ops.store.latest_clock_checks().get(d["id"])
    assert chk is not None and chk.status == "ok", chk
