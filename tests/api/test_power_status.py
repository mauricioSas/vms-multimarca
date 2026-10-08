"""`GET /api/status` trae la energía del PC en su propio campo `power` (CONTRATO §6.8), solo en Windows.

Sin esto, un portátil a batería o con la suspensión puesta dejaba de grabar sin que Estado del sistema dijera
nada (caso real del 8-oct-2026). La lectura de Windows se sustituye por datos inyectados.
"""
from __future__ import annotations

import pytest

from tests.api.conftest import Harness
from vms.ops import power
from vms.ops.power import PowerSnapshot

SHOP_LAPTOP = PowerSnapshot(on_battery=True, battery_percent=62, has_battery=True, lid_present=True,
                            modern_standby=True, sleep_ac_s=7200, sleep_dc_s=180, hibernate_ac_s=0,
                            hibernate_dc_s=57600, lid_action_ac=1, lid_action_dc=1)


async def test_status_reports_power_warnings_in_their_own_field(api: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "read_snapshot", lambda: SHOP_LAPTOP)
    op = await api.operator()
    st = (await op.get("/api/status")).json()
    p = st["power"]
    assert p["on_battery"] is True and p["battery_percent"] == 62
    assert p["sleep_ac_s"] == 7200 and p["sleep_dc_s"] == 180 and p["lid_action_ac"] == "sleep"
    assert [w["code"] for w in p["warnings"]] == ["on_battery", "idle_sleep_ac", "lid_ac"] and p["ok"] is False
    assert all("Qué hacer:" in w["message_es"] for w in p["warnings"])
    # sección aparte: no cambia el estado general ni los problemas del motor y las cámaras
    assert st["status"] == "ok" and st["problems"] == []
    # el kiosco no ve el estado del sistema
    assert (await (await api.kiosk()).get("/api/status")).status_code == 403


async def test_status_has_no_power_field_outside_windows(api: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "read_snapshot", lambda: None)
    st = (await (await api.login()).get("/api/status")).json()
    assert "power" not in st and st["status"] == "ok"
