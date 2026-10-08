"""Energía del PC en Estado del sistema (CONTRATO §6.8, campo `power`): avisos con los valores reales.

Caso real (8-oct-2026, portátil de tienda con espera moderna): dos huecos de grabación con el PC a batería
(Windows lo suspendía a los 3 min sin uso) y una noche enchufado con la tapa cerrada. Nada lo avisaba. Los
datos de energía se inyectan; la única prueba que lee Windows de verdad solo lee (sin administrador).
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any

import pytest

from tests.test_spanish_style import voseo_in
from vms.ops import power
from vms.ops.power import PowerSnapshot, evaluate, fmt_duration


def snap(**changes: Any) -> PowerSnapshot:
    """Portátil enchufado y bien configurado; cada prueba cambia solo lo que mira."""
    base: dict[str, Any] = dict(on_battery=False, battery_percent=100, has_battery=True, lid_present=True,
                                modern_standby=True, sleep_ac_s=0, sleep_dc_s=180, hibernate_ac_s=0,
                                hibernate_dc_s=57600, lid_action_ac=0, lid_action_dc=1)
    base.update(changes)
    return PowerSnapshot(**base)


def codes(result: dict[str, Any]) -> list[str]:
    return [w["code"] for w in result["warnings"]]


def message(result: dict[str, Any], code: str) -> str:
    texts = [w["message_es"] for w in result["warnings"] if w["code"] == code]
    assert len(texts) == 1, result["warnings"]
    text = texts[0]
    assert "Qué hacer:" in text and not voseo_in(text), text
    return text


def test_all_good_has_no_warnings() -> None:
    r = evaluate(snap())
    assert r["ok"] is True and r["warnings"] == []
    assert r["on_battery"] is False and r["sleep_ac_s"] == 0 and r["sleep_dc_s"] == 180
    assert r["lid_action_ac"] == "nothing" and r["lid_action_dc"] == "sleep"


def test_on_battery_warns_with_the_real_dc_timeout() -> None:
    r = evaluate(snap(on_battery=True, battery_percent=45))
    assert codes(r) == ["on_battery"] and r["ok"] is False
    text = message(r, "on_battery")
    assert text.startswith("El PC funciona con batería (queda un 45 %) y Windows lo suspende tras 3 min sin usar")
    assert "no se graba ninguna cámara" in text and text.endswith("Qué hacer: enchúfalo a la corriente.")
    # el plazo sale de Windows, no de un texto fijo
    assert "tras 15 min sin usar" in message(evaluate(snap(on_battery=True, sleep_dc_s=900)), "on_battery")
    # sin suspensión con batería: se apaga al agotarse; sin porcentaje conocido no se inventa
    text = message(evaluate(snap(on_battery=True, battery_percent=None, sleep_dc_s=0, hibernate_dc_s=0)),
                   "on_battery")
    assert "cuando se agote, se apagará" in text and "queda" not in text


def test_lid_action_sleep_on_ac_warns() -> None:
    r = evaluate(snap(lid_action_ac=1))
    assert codes(r) == ["lid_ac"]
    assert message(r, "lid_ac") == (
        "Al cerrar la tapa, Windows suspende el PC y deja de grabar. Qué hacer: en Panel de control > Opciones de "
        "energía > Elegir el comportamiento del cierre de la tapa, pon «No hacer nada» cuando esté enchufado.")
    assert "Windows hiberna el PC" in message(evaluate(snap(lid_action_ac=2)), "lid_ac")
    assert "Windows apaga el PC" in message(evaluate(snap(lid_action_ac=3)), "lid_ac")
    # un sobremesa no tiene tapa aunque su plan diga «suspender»; con batería la tapa no se avisa aparte
    assert evaluate(snap(lid_present=False, lid_action_ac=1))["warnings"] == []
    assert evaluate(snap(lid_action_dc=1))["warnings"] == []


def test_ac_standby_timeout_set_warns() -> None:
    r = evaluate(snap(sleep_ac_s=7200))
    assert codes(r) == ["idle_sleep_ac"]
    text = message(r, "idle_sleep_ac")
    assert text.startswith("Con corriente, Windows suspende este PC tras 2 h sin usar el teclado ni el ratón")
    assert "pon la suspensión en «Nunca» cuando esté enchufado" in text
    assert "tras 30 min sin usar" in message(evaluate(snap(sleep_ac_s=1800)), "idle_sleep_ac")
    # sin espera moderna (S3), «hibernar tras» también para el PC aunque no se suspenda
    text = message(evaluate(snap(modern_standby=False, hibernate_ac_s=3600)), "idle_sleep_ac")
    assert "Windows hiberna este PC tras 1 h" in text and "la hibernación en «Nunca»" in text
    # con espera moderna, la hibernación cuenta desde la suspensión: sin suspensión no hay parada
    assert evaluate(snap(modern_standby=True, hibernate_ac_s=3600))["warnings"] == []


def test_the_shop_laptop_of_8_october() -> None:
    # valores leídos en el PC de la tienda: STANDBYIDLE AC 7200 s y DC 180 s, LIDACTION AC = suspender
    r = evaluate(snap(on_battery=True, battery_percent=62, sleep_ac_s=7200, lid_action_ac=1))
    assert codes(r) == ["on_battery", "idle_sleep_ac", "lid_ac"]
    assert "tras 3 min" in message(r, "on_battery") and "tras 2 h" in message(r, "idle_sleep_ac")


def test_unreadable_values_never_invent_a_warning() -> None:
    unknown = PowerSnapshot(on_battery=None, battery_percent=None, has_battery=False, lid_present=False,
                            modern_standby=False, sleep_ac_s=None, sleep_dc_s=None, hibernate_ac_s=None,
                            hibernate_dc_s=None, lid_action_ac=None, lid_action_dc=None)
    r = evaluate(unknown)
    assert r["warnings"] == [] and r["on_battery"] is None and r["lid_action_ac"] is None


@pytest.mark.parametrize(("seconds", "text"), [(45, "45 s"), (90, "1 min 30 s"), (180, "3 min"), (7200, "2 h"),
                                               (5400, "1 h 30 min"), (57600, "16 h"), (3661, "1 h 1 min")])
def test_fmt_duration(seconds: int, text: str) -> None:
    assert fmt_duration(seconds) == text


def test_outside_windows_there_is_no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert power.read_snapshot() is None and power.power_status() is None


def test_a_read_failure_never_breaks_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> PowerSnapshot:
        raise OSError("powrprof.dll no responde")

    monkeypatch.setattr(power, "read_snapshot", broken)
    assert power.power_status() is None


@pytest.mark.skipif(sys.platform != "win32", reason="lee las API de energía de Windows")
def test_reads_this_pc_without_admin_and_without_changing_anything() -> None:
    assert ctypes.sizeof(power._Capabilities) == 76   # SYSTEM_POWER_CAPABILITIES
    s = power.read_snapshot()
    assert s is not None
    for value in (s.sleep_ac_s, s.sleep_dc_s, s.hibernate_ac_s, s.hibernate_dc_s):
        assert value is None or (isinstance(value, int) and value >= 0)
    assert s.lid_action_ac in (None, 0, 1, 2, 3) and s.lid_action_dc in (None, 0, 1, 2, 3)
    assert s.battery_percent is None or 0 <= s.battery_percent <= 100
    assert s.sleep_ac_s is not None, "el plan de energía activo se puede leer sin administrador"
    r = power.power_status()
    assert r is not None and r["sleep_ac_s"] == s.sleep_ac_s
    for w in r["warnings"]:
        message(r, w["code"])
