"""Interfaz del alta (devices.js) contra el backend REAL y mocks de marca: lo que el navegador manda al equipo.

- Probar con la contraseña mala y pulsar «Guardar» no vuelve a mandarla al importar canales (revisión B5:
  antes eran 2 intentos de los 5 que dan Hikvision y Dahua antes de bloquear el usuario).
- Si se corrige la contraseña tras la prueba, «Guardar» sí importa.
- Una propuesta de cambio de IP que el servidor no puede comprobar pide la contraseña en la propia fila.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine
from tests.vendors.conftest import TEST_PASSWORD
from tests.web.conftest import ADMIN, login, make_context, pw_browser  # noqa: F401 (fixtures)
from tests.web.demo import ServerThread
from tests.web.real_backend import RealBackend
from tests.web.stub_backend import StubOptions
from tools.mocks.hikvision import HikChannel, HikvisionMock
from vms.core.interfaces import DiscoveredDevice
from vms.vendors import client_for

import httpx

MOCKS: dict[str, HikvisionMock] = {}
FOUND: list[DiscoveredDevice] = []


def _factory(device: Any, password: str) -> Any:
    mock = MOCKS.get(device.host, MOCKS["default"])
    return client_for(device, password, transport=httpx.ASGITransport(app=mock.app))


async def _discover(timeout: float) -> list[DiscoveredDevice]:
    return list(FOUND)


@pytest.fixture
def ui(tmp_path: Path) -> Iterator[tuple[RealBackend, ServerThread]]:
    MOCKS.clear()
    FOUND.clear()
    MOCKS["default"] = HikvisionMock(password=TEST_PASSWORD, channels=[HikChannel("Entrada"), HikChannel("Cajas")])
    backend = RealBackend(tmp_path / "data", FakeEngine(), StubOptions(device_client=_factory, discover=_discover))
    server = ServerThread(backend.app).start()
    try:
        yield backend, server
    finally:
        server.stop()


def _fill_new_device(page: Any, password: str) -> None:
    page.click("#btn-add-device")
    page.fill("#dev-name", "NVR tienda")
    page.select_option("#dev-vendor", "hikvision")
    page.select_option("#dev-kind", "nvr")
    page.fill("#dev-host", "10.0.0.20")
    page.fill("#dev-password", password)


def _test_and_wait(page: Any) -> None:
    page.click("#btn-device-test")
    page.wait_for_selector("#device-test-result:not([hidden])")
    page.wait_for_function("() => !document.querySelector('#btn-device-test').disabled")


def _saved(backend: RealBackend) -> tuple[Any, int]:
    cfg = backend.app.state.vms.config()
    dev = next(d for d in cfg.devices if d.name == "NVR tienda")
    return dev, len(cfg.cameras_of(dev.id))


def test_save_after_refused_test_does_not_resend_the_password(ui: tuple[RealBackend, ServerThread],
                                                               make_context: Any) -> None:  # noqa: F811
    backend, server = ui
    page = make_context(server.base_url).new_page()
    login(page, ADMIN)
    _fill_new_device(page, "Mala#Pass:9@/x")
    _test_and_wait(page)
    assert "contraseña" in page.inner_text("#device-test-result").lower()
    assert MOCKS["default"].auth.credentialed == 1
    page.click("#btn-device-save")
    page.wait_for_selector("#device-dialog", state="hidden")
    _dev, cams = _saved(backend)
    assert cams == 0
    assert MOCKS["default"].auth.credentialed == 1          # «Guardar» no volvió a mandar la contraseña mala


def test_save_imports_when_the_password_was_corrected_after_the_test(ui: tuple[RealBackend, ServerThread],
                                                                     make_context: Any) -> None:  # noqa: F811
    backend, server = ui
    page = make_context(server.base_url).new_page()
    login(page, ADMIN)
    _fill_new_device(page, "Mala#Pass:9@/x")
    _test_and_wait(page)
    page.fill("#dev-password", TEST_PASSWORD)              # corrige la contraseña: ya no es la rechazada
    page.click("#btn-device-save")
    page.wait_for_selector("#device-dialog", state="hidden")
    _dev, cams = _saved(backend)
    assert cams == 2


def test_unverified_ip_move_asks_for_the_password_in_the_row(ui: tuple[RealBackend, ServerThread],
                                                            make_context: Any) -> None:  # noqa: F811
    backend, server = ui
    page = make_context(server.base_url).new_page()
    login(page, ADMIN)
    _fill_new_device(page, TEST_PASSWORD)
    page.click("#btn-device-save")
    page.wait_for_selector("#device-dialog", state="hidden")
    dev, _ = _saved(backend)
    mock = MOCKS["default"]
    # la identidad guardada es la de la API; un impostor en 10.0.0.81 anuncia la misma serie por SADP
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    async def set_identity() -> None:
        from vms.core.models import AppConfig, Device

        def mutate(cfg: AppConfig) -> None:
            data = cfg.device(dev.id).model_dump()  # type: ignore[union-attr]
            data["identity"] = {"serial": mock.serial, "mac": mock.mac, "source": "api"}
            cfg.devices = [Device.model_validate(data) if d.id == dev.id else d for d in cfg.devices]
        await backend.app.state.vms.update_config(mutate, "devices")

    with ThreadPoolExecutor(1) as pool:
        pool.submit(asyncio.run, set_identity()).result()
    MOCKS["10.0.0.81"] = HikvisionMock(password="no-la-sabe", serial="IMPOSTOR-000000000")
    FOUND.append(DiscoveredDevice(host="10.0.0.81", serial=mock.serial, sources=["sadp"]))
    page.click("#btn-discover")
    page.click("#btn-ip-check")
    page.wait_for_selector("[data-move='0']")
    assert page.locator("[data-move-pw='0']").count() == 0      # con vídeo: solo propuesta, sin comprobar aún
    page.click("[data-move='0']")
    page.wait_for_selector("[data-move-pw='0']")                # el servidor no pudo comprobarlo: pide la contraseña
    assert backend.app.state.vms.config().device(dev.id).host == "10.0.0.20"
    page.fill("[data-move-pw='0']", "Nueva#Clave:1")
    page.click("[data-move='0']")
    page.wait_for_function("() => !document.querySelector(\"[data-move='0']\")")
    assert backend.app.state.vms.config().device(dev.id).host == "10.0.0.81"
    assert backend.creds.get_device_password(dev.id) == "Nueva#Clave:1"
