"""Zona horaria de este PC como zona POSIX (`vms/core/tzposix.py`, CONTRATO §18.3).

La cámara del usuario tiene la zona de fábrica «CST-8:00:00» (China, UTC+8) y el PC está en Madrid (UTC+1, UTC+2 en
verano): para escribir la zona del PC en la cámara hace falta su regla POSIX exacta. En Windows sale de la zona que
usa el sistema; aquí se prueba con datos de Windows simulados y el reloj del «PC» simulado con tzdata, sin tocar
la configuración real.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from vms.core import tzposix
from vms.core.tzposix import PcZoneError, PosixZone, ZoneRule

MADRID = "CET-1CEST,M3.5.0,M10.5.0/3"
# GetDynamicTimeZoneInformation de un Windows en «(UTC+01:00) Bruselas, Copenhague, Madrid, París»
ROMANCE = (-60, 0, -60, (0, 10, 0, 5, 3, 0, 0, 0), (0, 3, 0, 5, 2, 0, 0, 0), "Romance Standard Time")
CHINA = (-480, 0, -60, (0, 0, 0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0, 0, 0), "China Standard Time")
H = timedelta(hours=1)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _pc_clock_in(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """El reloj de este PC, como si estuviera en la zona `name` (sin cambiar el del sistema)."""
    zone = ZoneInfo(name)
    monkeypatch.setattr(tzposix, "_machine_offset", lambda instant: instant.astimezone(zone).utcoffset())


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    tzposix._windows_zone.cache_clear()
    tzposix._posix_host_zone.cache_clear()


def test_madrid_from_tzdata_summer_and_winter() -> None:
    zone = tzposix.zone_from_iana("Europe/Madrid")
    assert zone.posix == MADRID and zone.name == "Europe/Madrid"
    assert zone.rule == PosixZone(H, 2 * H, ZoneRule(3, 5, 0, 2 * H), ZoneRule(10, 5, 0, 3 * H))
    assert zone.offset_at(_utc(2026, 7, 15, 10)) == 2 * H       # verano
    assert zone.offset_at(_utc(2026, 1, 15, 10)) == H           # invierno
    # los cambios de 2026, al segundo: 29 de marzo y 25 de octubre a la 01:00 UTC
    assert zone.offset_at(_utc(2026, 3, 29, 0, 59, 59)) == H and zone.offset_at(_utc(2026, 3, 29, 1)) == 2 * H
    assert zone.offset_at(_utc(2026, 10, 25, 0, 59, 59)) == 2 * H and zone.offset_at(_utc(2026, 10, 25, 1)) == H


def test_zone_without_dst_and_the_china_default() -> None:
    china = tzposix.zone_from_iana("Asia/Shanghai")
    assert china.posix == "CST-8" and not china.rule.has_dst and china.offset_at(_utc(2026, 7, 1)) == 8 * H
    india = tzposix.zone_from_iana("Asia/Kolkata")
    assert india.posix == "IST-5:30" and india.offset_at(_utc(2026, 1, 1)) == timedelta(hours=5, minutes=30)
    # la zona de fábrica tal como la da la cámara (formato de Hikvision) es la misma regla
    assert tzposix.posix_zone("CST-8:00:00") == china.rule


def test_tzif_footer_needs_version_2(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tzposix.tzif_footer(b"TZif2" + b"\x00" * 40 + b"\nCET-1CEST,M3.5.0,M10.5.0/3\n") == MADRID
    assert tzposix.tzif_footer(b"TZif\x00" + b"\x00" * 40 + b"\nCET-1\n") is None, "v1 no tiene pie"
    assert tzposix.tzif_footer(b"no es TZif") is None
    with pytest.raises(PcZoneError):
        tzposix.zone_from_iana("../../etc/passwd")
    # Dublín en tzdata «vanguard» (la del paquete tzdata): horario de verano negativo, no se adivina. Con el fichero
    # fijo: los TZif del sistema (Linux) pueden venir en formato «rearguard» («GMT0IST,M3.5.0/1,M10.5.0»), que sí vale
    monkeypatch.setattr(tzposix, "_tzif_bytes", lambda name: b"TZif2" + b"\x00" * 40 + b"\nIST-1GMT0,M10.5.0,M3.5.0/1\n")
    with pytest.raises(PcZoneError):
        tzposix.zone_from_iana("Europe/Dublin")


def test_windows_rules_become_the_posix_rule() -> None:
    bias, sb, db, std, dst, _ = ROMANCE
    assert tzposix.windows_zone_rule(bias, sb, db, std, dst) == tzposix.zone_from_iana("Europe/Madrid").rule
    bias, sb, db, std, dst, _ = CHINA
    assert tzposix.windows_zone_rule(bias, sb, db, std, dst) == PosixZone(8 * H)
    # hemisferio sur: «(UTC+10:00) Canberra, Melbourne, Sídney»
    aus = tzposix.windows_zone_rule(-600, 0, -60, (0, 4, 0, 1, 3, 0, 0, 0), (0, 10, 0, 1, 2, 0, 0, 0))
    assert aus == tzposix.zone_from_iana("Australia/Sydney").rule
    # 23:59:59,999 es medianoche (así da Windows algunos cambios)
    late = tzposix.windows_zone_rule(240, 0, -60, (0, 4, 6, 1, 23, 59, 59, 999), (0, 9, 6, 1, 23, 59, 59, 999))
    assert late.start is not None and late.start.at == timedelta(hours=24)
    with pytest.raises(PcZoneError, match="fecha fija"):
        tzposix.windows_zone_rule(-60, 0, -60, (2026, 10, 0, 25, 3, 0, 0, 0), (2026, 3, 0, 29, 2, 0, 0, 0))


def test_tzdata_name_for_windows_rules() -> None:
    bias, sb, db, std, dst, _ = ROMANCE
    assert tzposix.tzdata_posix_for(tzposix.windows_zone_rule(bias, sb, db, std, dst)) == MADRID
    assert tzposix.tzdata_posix_for(PosixZone(8 * H)) == "CST-8", "con letras antes que «<+08>-8»"
    odd = PosixZone(timedelta(hours=3, minutes=7))
    assert tzposix.tzdata_posix_for(odd) is None and odd.posix() == "<+0307>-3:07"


def test_generated_posix_strings_read_back_the_same() -> None:
    for name in ("Europe/Madrid", "America/New_York", "Australia/Sydney", "Australia/Lord_Howe", "Asia/Kolkata",
                 "Pacific/Chatham", "America/Santiago", "Etc/UTC"):
        rule = tzposix.zone_from_iana(name).rule
        assert tzposix.posix_zone(rule.posix()) == rule, name


def test_pc_zone_on_windows_in_madrid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(tzposix, "_windows_tzi", lambda: ROMANCE)
    _pc_clock_in(monkeypatch, "Europe/Madrid")
    for when in (_utc(2026, 7, 15, 10), _utc(2026, 1, 15, 10)):     # en verano y en invierno, la misma zona
        zone = tzposix.pc_zone(when)
        assert zone.posix == MADRID and zone.name == "Romance Standard Time"
    assert tzposix.pc_zone(_utc(2026, 7, 15)).offset_at(_utc(2026, 7, 15)) == 2 * H


def test_pc_zone_refused_when_it_does_not_match_the_pc_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si la regla no da el mismo desfase que el reloj de este PC durante el próximo año (p. ej. un cambio de hora que
    Windows aplica por fecha y no por regla), no se escribe en ningún equipo."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(tzposix, "_windows_tzi", lambda: ROMANCE)
    _pc_clock_in(monkeypatch, "Europe/Lisbon")       # el reloj del PC va una hora por detrás de esa regla
    with pytest.raises(PcZoneError) as err:
        tzposix.pc_zone(_utc(2026, 7, 15))
    assert "regla" in err.value.reason_es
    monkeypatch.setattr(tzposix, "_windows_tzi", lambda: None)
    with pytest.raises(PcZoneError, match="Windows no ha dicho"):
        tzposix.pc_zone(_utc(2026, 7, 15))


def test_pc_zone_from_tz_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    _pc_clock_in(monkeypatch, "Asia/Shanghai")
    zone = tzposix.pc_zone(_utc(2026, 7, 15))
    assert zone.posix == "CST-8" and zone.name == "Asia/Shanghai"
    monkeypatch.setenv("TZ", MADRID)                 # TZ con la regla POSIX directamente
    _pc_clock_in(monkeypatch, "Europe/Madrid")
    assert tzposix.pc_zone(_utc(2026, 7, 15)).posix == MADRID


def test_the_zone_of_this_pc_agrees_with_its_own_clock() -> None:
    """La zona real de este PC (o la de la máquina de CI) se puede escribir y coincide con su reloj."""
    now = datetime.now(timezone.utc)
    try:
        zone = tzposix.pc_zone(now)
    except PcZoneError as exc:                         # un PC con una zona que no se puede expresar: se dice por qué
        assert exc.reason_es
        return
    assert tzposix.posix_zone(zone.posix) == zone.rule
    assert tzposix.agrees_with_this_pc(zone.rule, now, days=40)
