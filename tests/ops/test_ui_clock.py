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
