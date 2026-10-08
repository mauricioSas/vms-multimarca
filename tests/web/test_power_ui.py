"""/status avisa si el PC funciona con batería, se suspende solo o se suspende al cerrar la tapa (CONTRATO §6.8).

Los datos de energía se inyectan (como `power` de /api/status); la página solo los muestra y no cambia nada.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine
from tests.test_spanish_style import voseo_in
from tests.web.conftest import OPERATOR, PageErrors, backend_class, login
from tests.web.demo import ServerThread
from tests.web.stub_backend import StubOptions, fake_discover
from vms.ops import power
from vms.ops.power import PowerSnapshot

ALLOWED = ("401 (Unauthorized)",)
SHOP_LAPTOP = PowerSnapshot(on_battery=True, battery_percent=62, has_battery=True, lid_present=True,
                            modern_standby=True, sleep_ac_s=7200, sleep_dc_s=180, hibernate_ac_s=0,
                            hibernate_dc_s=57600, lid_action_ac=1, lid_action_dc=1)
ALL_GOOD = PowerSnapshot(on_battery=False, battery_percent=100, has_battery=True, lid_present=True,
                         modern_standby=True, sleep_ac_s=0, sleep_dc_s=180, hibernate_ac_s=0,
                         hibernate_dc_s=57600, lid_action_ac=0, lid_action_dc=1)


@pytest.fixture
def power_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[dict[str, Any], ServerThread]]:
    """Backend de pruebas (o el real) con la lectura de Windows sustituida por `data["snap"]`."""
    data: dict[str, Any] = {"snap": SHOP_LAPTOP}
    monkeypatch.setattr(power, "read_snapshot", lambda: data["snap"])
    backend = backend_class()(tmp_path / "data", FakeEngine(),
                              StubOptions(discover=fake_discover, status_interval=1.0, power=power.power_status))
    server = ServerThread(backend.app).start()
    try:
        yield data, server
    finally:
        server.stop()


def test_status_page_shows_power_warnings_and_settings(power_stub: Any, make_context: Any) -> None:
    data, server = power_stub
    page = make_context(server.base_url).new_page()
    errors = PageErrors().attach(page)
    login(page, user=OPERATOR, next_path="/status")
    page.wait_for_selector("#power-root:not([hidden]) #h-st-power")
    alerts = page.locator("#power-alerts:not([hidden]) .banner")
    assert alerts.count() == 3
    texts = alerts.all_inner_texts()
    assert texts[0].startswith("El PC funciona con batería (queda un 62 %) y Windows lo suspende tras 3 min")
    assert texts[1].startswith("Con corriente, Windows suspende este PC tras 2 h")
    assert texts[2].startswith("Al cerrar la tapa, Windows suspende el PC y deja de grabar. Qué hacer:")
    assert all("Qué hacer:" in t and not voseo_in(t) for t in texts)
    # revisión final: arriba decía «Todo funciona con normalidad.» con estos tres avisos justo debajo
    overall = page.inner_text("#st-overall")
    assert overall.startswith("Atención: Windows puede suspender este PC") and "Qué hacer:" in overall, overall
    assert "Todo funciona con normalidad" not in overall
    details = page.inner_text("#power-root")
    assert "Con batería" in details and "queda un 62 %" in details
    assert "Se suspende tras 2 h sin uso" in details and "Se suspende tras 3 min sin uso" in details
    assert "hiberna tras 16 h" in details and "Suspender" in details
    assert page.locator("#power-root .pill.warn").count() == 4   # batería, con corriente, con batería, tapa
    assert not voseo_in(details)

    # enchufado y bien configurado: sin avisos, la sección sigue con los valores
    data["snap"] = ALL_GOOD
    page.click("#st-refresh")
    page.wait_for_selector("#power-alerts", state="hidden")
    details = page.inner_text("#power-root")
    assert "Enchufado a la corriente" in details and "No se suspende nunca" in details
    assert "No hacer nada" in details and page.locator("#power-root .pill.warn").count() == 0
    assert page.inner_text("#st-overall").startswith("Todo funciona con normalidad.")

    # fuera de Windows no hay comprobación: no se pinta nada
    data["snap"] = None
    page.click("#st-refresh")
    page.wait_for_selector("#power-root", state="hidden")
    assert page.is_hidden("#power-alerts")
    assert errors.unexpected(ALLOWED) == []
