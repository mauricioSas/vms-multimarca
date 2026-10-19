"""«Poner en hora con este PC» desde Estado del sistema, en Chromium contra el backend REAL (CONTRATO §18.3).

El equipo es el simulador de Hikvision con la lectura real de la cámara del usuario (01-01-1970 en UTC+8, hora
puesta a mano) y un usuario del VMS de solo lectura, como recomienda ALTA-EQUIPOS.md. Antes la tabla decía
«-1791489509,3 s» y «Activa NTP en el equipo apuntando a este PC», sin ningún botón para arreglarlo.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fakes import FakeEngine
from tests.ops.test_ui_b6 import ADMIN, CONTEXTS, Errors, _api, _login, _page
from tests.web.demo import ServerThread
from tests.web.real_backend import RealBackend
from tools.mocks.hikvision import HikvisionMock
from vms.vendors import client_for

pytestmark = pytest.mark.needs_browser
READ_PW = "Lectura#2026"
ADMIN_PW = "Adm#Temporal:9"


def test_set_clock_from_the_status_page(browser: Any, tmp_path: Path) -> None:
    backend = RealBackend(tmp_path / "datos", FakeEngine())
    srv = ServerThread(backend.app).start()
    try:
        c = _api(srv)
        c.put("/api/onboarding/state", json={"wizard_completed": True})
        dev = c.post("/api/devices", json={"name": "Cámara caja", "vendor": "hikvision", "kind": "camera",
                                           "host": "192.0.2.10", "username": "vms", "password": READ_PW,
                                           "import_channels": [1]}).json()
        real = datetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=8)))
        mock = HikvisionMock(kind="camera", username="vms", password=READ_PW, extra_users={"admin": ADMIN_PW},
                             write_users={"admin"}, time_mode="manual", tz_hours=8,
                             clock_offset_s=(real - datetime.now(timezone.utc)).total_seconds())
        backend.app.state.vms.client_factory = lambda d, p: client_for(d, p, transport=httpx.ASGITransport(app=mock.app))
        assert c.post("/api/clock/check").status_code == 200

        page = _page(browser, srv)
        errs = Errors(page)
        _login(page, ADMIN, "/status")
        row = page.locator(f"#ops-clock-table tr[data-device='{dev['id']}']")
        row.wait_for()
        assert "−57 años" in row.inner_text() and "no tiene la hora puesta" in row.inner_text()
        page.click(f"button[data-clock-set='{dev['id']}']")
        # la hora que se escribe es la del PC del programa, no la del navegador (puede ser otro ordenador)
        assert "la hora del PC donde está instalado el programa" in page.inner_text(".ops-modal")
        # revisión final: el botón del diálogo decía «Poner en hora» y los errores, «pulsa otra vez «Poner en hora
        # con este PC»», que no estaba en pantalla
        assert page.inner_text(".ops-modal button.btn-primary").strip() == "Poner en hora con este PC"
        page.click(".ops-modal button.btn-primary")
        page.wait_for_selector(".ops-clock-result .form-error")
        assert "solo lectura" in page.inner_text(".ops-clock-result") and mock.time_puts, "lo intentó con el guardado"
        assert page.locator(".ops-clock-admin[open]").count() == 1, "pide el administrador del equipo"
        # revisión: solo la contraseña (el usuario se ve como «admin» en gris, pero es el texto de ejemplo). Antes se
        # mandaba sin ninguno de los dos, con el usuario guardado: el mismo «solo lectura», la contraseña borrada y
        # otro intento con el usuario de solo lectura
        user_box = ".ops-modal input[aria-label='Usuario administrador del equipo']"
        pass_box = ".ops-modal input[aria-label='Contraseña de administrador del equipo']"
        sent, puts = len(mock.requests), len(mock.time_puts)
        for typed_user in ("", "   "):
            page.fill(user_box, typed_user)
            page.fill(pass_box, ADMIN_PW)
            page.click(".ops-modal button.btn-primary")
            page.locator(".ops-clock-result .form-error", has_text="si dejas los dos vacíos").wait_for()
            assert "Escribe el usuario y la contraseña de administrador" in page.inner_text(".ops-clock-result")
            assert (len(mock.requests), len(mock.time_puts)) == (sent, puts), "no se manda nada al equipo"
            assert page.input_value(pass_box) == ADMIN_PW, "no se borra lo escrito"
        page.fill(user_box, "admin")
        page.fill(pass_box, ADMIN_PW)
        page.click(".ops-modal button.btn-primary")
        page.wait_for_selector(".ops-clock-result .banner")
        assert "ya está en hora" in page.inner_text(".ops-clock-result")
        assert page.input_value(".ops-modal input[type='password']") == "", "la contraseña no se queda en el campo"
        assert abs(c.get("/api/clock").json()["devices"][0]["skew_s"]) < 2
        assert [e for e in errs.unexpected() if "403" not in e] == []
    finally:
        while CONTEXTS:
            CONTEXTS.pop().close()
        srv.stop()


def test_dialog_offers_the_pc_zone_when_it_differs(browser: Any, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """La cámara del usuario tiene la zona de fábrica de China (UTC+8) y el PC la de Madrid: el diálogo lo dice en
    claro y trae marcada «Usar también la zona horaria de este PC»; lo que manda la web lleva `use_pc_zone`. Con la
    misma zona, la casilla sale sin marcar y no se manda."""
    from zoneinfo import ZoneInfo

    from vms.core import tzposix
    from vms.ops.health import clock as clockmod
    madrid = ZoneInfo("Europe/Madrid")
    zone = tzposix.zone_from_iana("Europe/Madrid")
    monkeypatch.setattr(tzposix, "pc_zone", lambda now=None: zone)
    monkeypatch.setattr(clockmod, "_pc_offset_s", lambda instant: 7200)       # el PC, en horario de verano
    backend = RealBackend(tmp_path / "datos", FakeEngine())
    srv = ServerThread(backend.app).start()
    try:
        c = _api(srv)
        c.put("/api/onboarding/state", json={"wizard_completed": True})
        dev = c.post("/api/devices", json={"name": "Cámara caja", "vendor": "hikvision", "kind": "camera",
                                           "host": "192.0.2.10", "username": "admin", "password": READ_PW,
                                           "import_channels": [1]}).json()
        real = datetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=8)))
        mock = HikvisionMock(kind="camera", username="admin", password=READ_PW, time_mode="manual", tz_hours=8,
                             clock_offset_s=(real - datetime.now(timezone.utc)).total_seconds())
        backend.app.state.vms.client_factory = lambda d, p: client_for(d, p, transport=httpx.ASGITransport(app=mock.app))
        assert c.post("/api/clock/check").status_code == 200

        page = _page(browser, srv)
        errs = Errors(page)
        sent: list[Any] = []
        page.on("request", lambda req: sent.append(req.post_data_json) if req.url.endswith(f"/{dev['id']}/set") else None)
        _login(page, ADMIN, "/status")
        page.click(f"button[data-clock-set='{dev['id']}']")
        text = page.inner_text(".ops-modal")
        assert "El equipo usa la zona horaria UTC+8 y este PC, UTC+2" in text and "6 h adelantada" in text, text
        assert "Usar también la zona horaria de este PC (UTC+1 en invierno y UTC+2 en verano)" in text
        box = page.locator(".ops-modal input[name='use_pc_zone']")
        assert box.is_checked(), "marcada porque las zonas no coinciden"
        page.click(".ops-modal button.btn-primary")
        page.wait_for_selector(".ops-clock-result .banner")
        assert "Ya usa la zona horaria de este PC" in page.inner_text(".ops-clock-result")
        assert sent == [{"use_pc_zone": True}]
        assert "<timeZone>CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00</timeZone>" in mock.time_puts[0]

        # ya con la zona de Madrid (y la hora otra vez desajustada): la casilla sale sin marcar y no se manda
        mock.clock_offset_s = -600.0
        page.click(".ops-modal button:has-text('Cerrar')")
        assert c.post("/api/clock/check").json()["devices"][0]["utc_offset_s"] == \
            int(datetime.now(madrid).utcoffset().total_seconds())   # type: ignore[union-attr]
        monkeypatch.setattr(clockmod, "_pc_offset_s",
                            lambda instant: int(instant.astimezone(madrid).utcoffset().total_seconds()))  # type: ignore[union-attr]
        page.reload()
        page.click(f"button[data-clock-set='{dev['id']}']")
        assert "ya usa la misma zona horaria que este PC" in page.inner_text(".ops-modal")
        assert not page.locator(".ops-modal input[name='use_pc_zone']").is_checked()
        page.click(".ops-modal button.btn-primary")
        page.wait_for_selector(".ops-clock-result .banner")
        assert sent[-1] is None or "use_pc_zone" not in sent[-1], sent
        assert len(mock.time_puts) == 2 and "<timeZone>CST-1:00:00DST01:00:00" in mock.time_puts[1]
        assert errs.unexpected() == []
    finally:
        while CONTEXTS:
            CONTEXTS.pop().close()
        srv.stop()


def _china_camera_page(browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pc_zone: Any,
                       **mock_kw: Any) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Backend real con la cámara del usuario (zona de China, 01-01-1970, hora a mano) y el PC en Madrid en verano;
    `pc_zone` sustituye a `tzposix.pc_zone`. Devuelve el servidor, el simulador, la página y el equipo."""
    from vms.core import tzposix
    from vms.ops.health import clock as clockmod
    monkeypatch.setattr(tzposix, "pc_zone", pc_zone)
    monkeypatch.setattr(clockmod, "_pc_offset_s", lambda instant: 7200)       # el PC, en horario de verano
    backend = RealBackend(tmp_path / "datos", FakeEngine())
    srv = ServerThread(backend.app).start()
    c = _api(srv)
    c.put("/api/onboarding/state", json={"wizard_completed": True})
    dev = c.post("/api/devices", json={"name": "Cámara caja", "vendor": "hikvision", "kind": "camera",
                                       "host": "192.0.2.10", "username": "admin", "password": READ_PW,
                                       "import_channels": [1]}).json()
    real = datetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=8)))
    mock = HikvisionMock(kind="camera", username="admin", password=READ_PW, time_mode="manual", tz_hours=8,
                         clock_offset_s=(real - datetime.now(timezone.utc)).total_seconds(), **mock_kw)
    backend.app.state.vms.client_factory = lambda d, p: client_for(d, p, transport=httpx.ASGITransport(app=mock.app))
    assert c.post("/api/clock/check").status_code == 200
    return srv, mock, _page(browser, srv), dev


def test_zone_refused_by_the_camera_unticks_the_box(browser: Any, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión: un firmware que rechaza el PUT con la zona nueva (`badParameters`) daba 502 «ponla en hora desde la
    web» y la casilla, marcada por defecto con la zona de China, seguía marcada: cada clic fallaba igual. Ahora el
    mensaje dice que no se ha cambiado nada y que se pulse sin la casilla, la casilla se desmarca sola y el siguiente
    clic pone la hora sin tocar la zona."""
    from vms.core import tzposix
    zone = tzposix.zone_from_iana("Europe/Madrid")
    srv, mock, page, dev = _china_camera_page(browser, tmp_path, monkeypatch, lambda now=None: zone,
                                              reject_zone_http=200)
    try:
        errs = Errors(page)
        sent: list[Any] = []
        page.on("request", lambda req: sent.append(req.post_data_json) if req.url.endswith(f"/{dev['id']}/set") else None)
        _login(page, ADMIN, "/status")
        page.click(f"button[data-clock-set='{dev['id']}']")
        box = page.locator(".ops-modal input[name='use_pc_zone']")
        assert box.is_checked(), "marcada porque las zonas no coinciden"
        page.click(".ops-modal button.btn-primary")
        page.wait_for_selector(".ops-clock-result .form-error")
        text = page.inner_text(".ops-clock-result")
        assert "No se ha cambiado nada" in text and "sin marcar la casilla «Usar también la zona horaria de este " \
            "PC»" in text, text
        assert not box.is_checked(), "se desmarca para el siguiente clic"
        page.click(".ops-modal button.btn-primary")
        page.wait_for_selector(".ops-clock-result .banner")
        assert "ya está en hora" in page.inner_text(".ops-clock-result")
        assert sent[0] == {"use_pc_zone": True} and (sent[1] is None or "use_pc_zone" not in sent[1]), sent
        assert len(mock.time_puts) == 2 and "<timeZone>CST-8:00:00</timeZone>" in mock.time_puts[1]
        assert [e for e in errs.unexpected() if "422" not in e] == []
    finally:
        while CONTEXTS:
            CONTEXTS.pop().close()
        srv.stop()


def test_dialog_without_the_box_does_not_mention_it(browser: Any, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión: si la zona de este PC no se puede escribir, el diálogo no trae la casilla, pero decía «solo cambia si
    marcas la casilla de abajo»."""
    from vms.core import tzposix

    def no_zone(now: Any = None) -> Any:
        raise tzposix.PcZoneError("Windows da el cambio de hora de este PC con una fecha fija")
    srv, mock, page, dev = _china_camera_page(browser, tmp_path, monkeypatch, no_zone)
    try:
        errs = Errors(page)
        _login(page, ADMIN, "/status")
        page.click(f"button[data-clock-set='{dev['id']}']")
        text = page.inner_text(".ops-modal")
        assert page.locator(".ops-modal input[name='use_pc_zone']").count() == 0 and "fecha fija" in text
        assert "casilla" not in text and "Su zona horaria no cambia, ni ningún otro ajuste." in text, text
        assert errs.unexpected() == []
    finally:
        while CONTEXTS:
            CONTEXTS.pop().close()
        srv.stop()


def test_no_button_when_another_program_sets_the_clock(browser: Any, tmp_path: Path) -> None:
    """Revisión: con la hora puesta por HikCentral/iVMS (modo SDK, que se lee como «unknown») el botón salía y el
    servidor siempre lo rechazaba con 409. Ahora solo sale con la hora puesta a mano."""
    backend = RealBackend(tmp_path / "datos", FakeEngine())
    srv = ServerThread(backend.app).start()
    try:
        c = _api(srv)
        c.put("/api/onboarding/state", json={"wizard_completed": True})
        dev = c.post("/api/devices", json={"name": "Cámara caja", "vendor": "hikvision", "kind": "camera",
                                           "host": "192.0.2.10", "username": "vms", "password": READ_PW,
                                           "import_channels": [1]}).json()
        real = datetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=8)))
        mock = HikvisionMock(kind="camera", username="vms", password=READ_PW, time_mode="SDK", tz_hours=8,
                             clock_offset_s=(real - datetime.now(timezone.utc)).total_seconds())
        backend.app.state.vms.client_factory = lambda d, p: client_for(d, p, transport=httpx.ASGITransport(app=mock.app))
        assert c.post("/api/clock/check").json()["devices"][0]["status"] == "critical"

        page = _page(browser, srv)
        _login(page, ADMIN, "/status")
        row = page.locator(f"#ops-clock-table tr[data-device='{dev['id']}']")
        row.wait_for()
        assert "HikCentral" in row.inner_text()
        assert page.locator(f"button[data-clock-set='{dev['id']}']").count() == 0
    finally:
        while CONTEXTS:
            CONTEXTS.pop().close()
        srv.stop()
