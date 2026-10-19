"""Zonas horarias en formato POSIX (CONTRATO §18.3): leerlas, evaluarlas y saber cuál usa ESTE PC.

- `posix_zone()` entiende la forma POSIX («CET-1CEST,M3.5.0,M10.5.0/3», la de ONVIF) y la de Hikvision
  («CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00»). En las dos el signo va al revés que en ISO.
- `pc_zone()` da la zona de este PC como cadena POSIX, para escribirla en un equipo junto con la hora («Usar
  también la zona horaria de este PC»):
  - En Windows sale de la zona que usa el sistema (`GetDynamicTimeZoneInformation`: desfase y reglas del cambio de
    hora). Después se busca en tzdata (IANA) la cadena POSIX con esas mismas reglas, así se escribe
    «CET-1CEST,M3.5.0,M10.5.0/3» y no un nombre inventado.
  - Fuera de Windows, del pie POSIX del fichero TZif de la zona (`TZ` o `/etc/localtime`).
  - Antes de darla por buena se comprueba, hora a hora durante el próximo año, que da el mismo desfase que el reloj
    de este PC. Si no se puede expresar así (cambio de hora por fecha fija, reglas que no se entienden…), lanza
    `PcZoneError` con el motivo y no se escribe nada.

Va en `vms.core` porque la usan los drivers (B5) y el servicio de operación (B6), que no importa los drivers.
"""
from __future__ import annotations

import calendar
import functools
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("vms.core.tzposix")

_POSIX_RE = re.compile(r"^[A-Za-z]{3,}([+-]?)(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?")
_NAME = r"(?:[A-Za-z]{3,}|<[^>]+>)"
_OFF = r"[+-]?\d{1,2}(?::\d{2}){0,2}"
_FULL_RE = re.compile(rf"^(?P<std>{_NAME})(?P<stdoff>{_OFF})(?:(?P<dst>{_NAME})(?P<dstoff>{_OFF})?"
                      r"(?:,(?P<start>[^,]+),(?P<end>[^,]+))?)?$")
_RULE_RE = re.compile(r"^M(\d{1,2})\.(\d)\.(\d)(?:/([+-]?\d{1,3}(?::\d{2}){0,2}))?$")
_IANA_RE = re.compile(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")
_DEFAULT_AT = timedelta(hours=2)          # POSIX: sin hora, el cambio es a las 02:00
CHECK_DAYS = 366                          # la zona tiene que coincidir con el reloj de este PC durante un año


def _hms(text: str) -> timedelta:
    sign = -1 if text.startswith("-") else 1
    parts = [int(p) for p in text.lstrip("+-").split(":")]
    parts += [0] * (3 - len(parts))
    return sign * timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2])


def fmt_hms(value: timedelta, *, hours_width: int = 1, short: bool = False) -> str:
    """timedelta → «H:MM:SS» (con signo «-» si es negativo). `short`: sin los ceros de la derecha («3», «2:30»)."""
    total = int(value.total_seconds())
    sign, total = ("-" if total < 0 else ""), abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if short:
        return f"{sign}{h}" + (f":{m:02d}" if m or s else "") + (f":{s:02d}" if s else "")
    return f"{sign}{h:0{hours_width}d}:{m:02d}:{s:02d}"


def posix_offset(tz: str) -> timezone | None:
    """«CST-1:00:00» → UTC+1 (POSIX invierte el signo). None si no se entiende. Solo la parte estándar."""
    m = _POSIX_RE.match(tz.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return timezone(timedelta(minutes=-sign * minutes))


@dataclass(frozen=True)
class ZoneRule:
    """Regla «Mm.w.d/hora» de un cambio de hora."""

    month: int
    week: int          # 1..5 (5 = la última)
    weekday: int       # 0 = domingo (POSIX)
    at: timedelta      # hora local del cambio

    def when(self, year: int) -> datetime:
        py_wd = (self.weekday - 1) % 7                      # POSIX domingo=0 → Python lunes=0
        first = date(year, self.month, 1)
        day = 1 + (py_wd - first.weekday()) % 7 + 7 * (self.week - 1)
        last = calendar.monthrange(year, self.month)[1]
        while day > last:
            day -= 7
        return datetime(year, self.month, day) + self.at

    def posix(self) -> str:
        """«M3.5.0» o «M10.5.0/3» (la hora solo si no es la de por defecto, las 02:00)."""
        base = f"M{self.month}.{self.week}.{self.weekday}"
        return base if self.at == _DEFAULT_AT else f"{base}/{fmt_hms(self.at, short=True)}"


def _rule(text: str) -> ZoneRule | None:
    m = _RULE_RE.match(text.strip())
    if not m:
        return None   # formas «Jn» y «n» (día juliano): no se usan en estos equipos; no se adivinan
    month, week, wd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= week <= 5 and 0 <= wd <= 6):
        return None
    return ZoneRule(month, week, wd, _hms(m.group(4)) if m.group(4) else _DEFAULT_AT)


@dataclass(frozen=True)
class PosixZone:
    """Zona POSIX con su regla de horario de verano (offsets en sentido ISO: UTC+1 = +1 h)."""

    std: timedelta
    dst: timedelta | None = None
    start: ZoneRule | None = None
    end: ZoneRule | None = None

    @property
    def has_dst(self) -> bool:
        return self.dst is not None

    def _in_dst_local(self, local: datetime, *, standard: bool = False) -> bool | None:
        """¿Es horario de verano a esa hora local (sin zona)? None si hay DST pero no se entiende la regla.

        El inicio se expresa en hora estándar y el fin en hora de verano (POSIX). Con `standard=True`, `local` es
        la hora estándar (sin el adelanto): así un instante UTC se resuelve sin la hora ambigua del cambio."""
        if self.dst is None:
            return False
        if self.start is None or self.end is None:
            return None
        start, end = self.start.when(local.year), self.end.when(local.year)
        if standard:
            end -= self.dst - self.std
        if start < end:                                     # hemisferio norte
            return start <= local < end
        return local >= start or local < end                # hemisferio sur

    def offset_at_local(self, local: datetime) -> timedelta | None:
        """Offset de una hora local (naive) del equipo. None si no se puede saber."""
        dst = self._in_dst_local(local.replace(tzinfo=None))
        if dst is None:
            return None
        return self.dst if dst and self.dst is not None else self.std

    def offset_at_utc(self, utc: datetime) -> timedelta | None:
        """Offset en un instante UTC. None si no se puede saber."""
        local_std = utc.astimezone(timezone.utc).replace(tzinfo=None) + self.std
        dst = self._in_dst_local(local_std, standard=True)
        if dst is None:
            return None
        return self.dst if dst and self.dst is not None else self.std

    def posix(self, std_name: str = "", dst_name: str = "") -> str:
        """Cadena POSIX («CET-1CEST,M3.5.0,M10.5.0/3»). Sin nombres, los numéricos de tzdata («<+01>-1<+02>…»)."""
        def name(off: timedelta, given: str) -> str:
            if given:
                return given
            total = int(off.total_seconds())
            h, rem = divmod(abs(total), 3600)
            return f"<{'+' if total >= 0 else '-'}{h:02d}{f'{rem // 60:02d}' if rem else ''}>"

        text = f"{name(self.std, std_name)}{fmt_hms(-self.std, short=True)}"
        if self.dst is None or self.start is None or self.end is None:
            return text
        text += name(self.dst, dst_name)
        if self.dst - self.std != timedelta(hours=1):
            text += fmt_hms(-self.dst, short=True)
        return f"{text},{self.start.posix()},{self.end.posix()}"


def posix_zone(tz: str) -> PosixZone | None:
    """«CET-1CEST,M3.5.0,M10.5.0/3» o la forma de Hikvision «CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00».

    En POSIX el offset del horario de verano, si se escribe, es otro offset POSIX («CEST-2»); Hikvision escribe en
    su lugar lo que se ADELANTA («DST01:00:00» = una hora más). Se distingue por el resultado: un horario de verano
    de 0 a 2 h por delante del estándar es lo único razonable."""
    m = _FULL_RE.match((tz or "").strip())
    if not m:
        return None
    std = -_hms(m.group("stdoff"))
    if not m.group("dst"):
        return PosixZone(std)
    dst = std + timedelta(hours=1)
    if m.group("dstoff"):
        raw = _hms(m.group("dstoff"))
        posix_dst = -raw
        if timedelta(0) < posix_dst - std <= timedelta(hours=2):
            dst = posix_dst                                  # POSIX de verdad («CEST-2»)
        elif timedelta(0) < raw <= timedelta(hours=2):
            dst = std + raw                                  # Hikvision: lo que se adelanta
        else:
            return PosixZone(std, dst, None, None)           # no se entiende: offset desconocido en verano
    start = _rule(m.group("start")) if m.group("start") else None
    end = _rule(m.group("end")) if m.group("end") else None
    return PosixZone(std, dst, start, end)


# --------------------------------------------------------------------------- zona de este PC
class PcZoneError(Exception):
    """La zona horaria de este PC no se puede expresar como zona POSIX (o no se ha podido saber cuál es).
    `reason_es`: el motivo en lenguaje de tienda, sin «Qué hacer:» (lo pone quien lo muestra)."""

    def __init__(self, reason_es: str) -> None:
        super().__init__(reason_es)
        self.reason_es = reason_es


@dataclass(frozen=True)
class PcZone:
    """Zona horaria de este PC: la cadena POSIX que se escribe en los equipos y sus reglas."""

    posix: str              # «CET-1CEST,M3.5.0,M10.5.0/3»
    rule: PosixZone
    name: str = ""          # «Europe/Madrid» o la clave de Windows («Romance Standard Time»); solo informativo

    def offset_at(self, instant: datetime) -> timedelta:
        off = self.rule.offset_at_utc(instant)
        return off if off is not None else self.rule.std


def tzif_footer(data: bytes) -> str | None:
    """Pie POSIX de un fichero TZif v2 o posterior («\\nCET-1CEST,M3.5.0,M10.5.0/3\\n» al final). None si no lo hay."""
    if data[:4] != b"TZif" or data[4:5] < b"2" or not data.endswith(b"\n"):
        return None
    start = data.rfind(b"\n", 0, len(data) - 1)
    if start < 0:
        return None
    footer = data[start + 1:-1].decode("ascii", errors="replace").strip()
    return footer or None


def _tzif_bytes(name: str) -> bytes | None:
    """Fichero TZif de una zona IANA: las rutas del sistema (`zoneinfo.TZPATH`) y, si no, el paquete `tzdata`
    (en Windows es la única fuente; va en los locks de producción)."""
    if not _IANA_RE.match(name or "") or ".." in name:
        return None
    import zoneinfo
    for root in zoneinfo.TZPATH:
        path = Path(root, *name.split("/"))
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError:
            continue
    try:
        from importlib import resources
        res = resources.files("tzdata").joinpath("zoneinfo", *name.split("/"))
        return res.read_bytes() if res.is_file() else None
    except (ImportError, OSError, ValueError):
        return None


def zone_from_iana(name: str) -> PcZone:
    """Zona IANA («Europe/Madrid») → `PcZone` con el pie POSIX de su fichero TZif (las reglas de ahora en adelante)."""
    data = _tzif_bytes(name)
    if data is None:
        raise PcZoneError(f"no se encuentra la zona horaria «{name[:64]}» en los datos de zonas de este PC")
    return _from_posix(tzif_footer(data) or "", name)


def _from_posix(text: str, name: str = "") -> PcZone:
    rule = posix_zone(text)
    if rule is None or (rule.has_dst and (rule.start is None or rule.end is None)):
        raise PcZoneError(f"su regla de cambio de hora ({text[:64] or 'vacía'}) no se puede escribir en un equipo")
    return PcZone(text, rule, name)


SystemTime = tuple[int, int, int, int, int, int, int, int]   # wYear, wMonth, wDayOfWeek, wDay, wHour, wMinute, wSecond, wMs


def _windows_rule(st: SystemTime) -> ZoneRule:
    year, month, weekday, week, hour, minute, second, ms = st
    if year:
        raise PcZoneError("Windows da el cambio de hora de este PC con una fecha fija, no con una regla de cada año")
    if not (1 <= month <= 12 and 1 <= week <= 5 and 0 <= weekday <= 6):
        raise PcZoneError("Windows da una regla de cambio de hora que no se entiende")
    at = timedelta(hours=hour, minutes=minute, seconds=second, milliseconds=ms)
    return ZoneRule(month, week, weekday, timedelta(seconds=round(at.total_seconds())))   # 23:59:59,999 = 24:00


def windows_zone_rule(bias: int, standard_bias: int, daylight_bias: int, standard_date: SystemTime,
                      daylight_date: SystemTime) -> PosixZone:
    """Reglas de `TIME_ZONE_INFORMATION` de Windows → `PosixZone`. `Bias` va en minutos y al revés (UTC = local +
    Bias); el cambio a horario de verano (`DaylightDate`) se da en hora estándar y la vuelta (`StandardDate`) en hora
    de verano, igual que en POSIX; `wDay` es la semana del mes (5 = la última) y `wDayOfWeek` 0 = domingo."""
    std = -timedelta(minutes=bias + standard_bias)
    dst = -timedelta(minutes=bias + daylight_bias)
    if not daylight_date[1] or not standard_date[1] or dst == std:
        return PosixZone(std)                    # sin cambio de hora (o desactivado en Windows)
    return PosixZone(std, dst, _windows_rule(daylight_date), _windows_rule(standard_date))


def _windows_tzi() -> tuple[int, int, int, SystemTime, SystemTime, str] | None:
    """`GetDynamicTimeZoneInformation` (solo lectura del sistema). None fuera de Windows o si falla."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _SystemTime(ctypes.Structure):
        _fields_ = [(n, wintypes.WORD) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute",
                                                  "wSecond", "wMilliseconds")]

    class _Dtzi(ctypes.Structure):
        _fields_ = [("Bias", wintypes.LONG), ("StandardName", wintypes.WCHAR * 32), ("StandardDate", _SystemTime),
                    ("StandardBias", wintypes.LONG), ("DaylightName", wintypes.WCHAR * 32),
                    ("DaylightDate", _SystemTime), ("DaylightBias", wintypes.LONG),
                    ("TimeZoneKeyName", wintypes.WCHAR * 128), ("DynamicDaylightTimeDisabled", wintypes.BOOLEAN)]

    info = _Dtzi()
    try:
        get = ctypes.WinDLL("kernel32", use_last_error=True).GetDynamicTimeZoneInformation
        get.argtypes = [ctypes.POINTER(_Dtzi)]
        get.restype = wintypes.DWORD
        ret = get(ctypes.byref(info))
    except (AttributeError, OSError):
        return None
    if ret == 0xFFFFFFFF:                      # TIME_ZONE_ID_INVALID
        return None

    def st(s: _SystemTime) -> SystemTime:
        return (s.wYear, s.wMonth, s.wDayOfWeek, s.wDay, s.wHour, s.wMinute, s.wSecond, s.wMilliseconds)
    return (int(info.Bias), int(info.StandardBias), int(info.DaylightBias), st(info.StandardDate),
            st(info.DaylightDate), str(info.TimeZoneKeyName))


@functools.lru_cache(maxsize=8)
def tzdata_posix_for(rule: PosixZone) -> str | None:
    """La cadena POSIX de tzdata con exactamente estas reglas (con nombres de letras si las hay: «CET-1CEST…» antes
    que «<+01>-1…»), o None. Se lee una vez por regla (unas 600 zonas, unas décimas de segundo)."""
    import zoneinfo
    counts: Counter[str] = Counter()
    try:
        names = zoneinfo.available_timezones()
    except Exception:  # noqa: BLE001 - sin datos de zonas se usan los nombres numéricos
        return None
    for name in names:
        data = _tzif_bytes(name)
        footer = tzif_footer(data) if data else None
        if footer and posix_zone(footer) == rule:
            counts[footer] += 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: ("<" in kv[0], -kv[1], kv[0]))[0][0]


def _machine_offset(instant: datetime) -> timedelta | None:
    try:
        return instant.astimezone().utcoffset()
    except (OSError, OverflowError, ValueError):
        return None


def agrees_with_this_pc(rule: PosixZone, start: datetime, days: int = CHECK_DAYS) -> bool:
    """¿La regla da el mismo desfase que el reloj de este PC en cada hora en punto de los próximos `days` días?"""
    t0 = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for h in range(days * 24):
        instant = t0 + timedelta(hours=h)
        want = rule.offset_at_utc(instant)
        if want is None or want != _machine_offset(instant):
            return False
    return True


@functools.lru_cache(maxsize=4)
def _windows_zone(tzi: tuple[int, int, int, SystemTime, SystemTime, str], day: date) -> PcZone:
    bias, std_bias, dst_bias, std_date, dst_date, key = tzi
    rule = windows_zone_rule(bias, std_bias, dst_bias, std_date, dst_date)
    _check(rule, day)
    return PcZone(tzdata_posix_for(rule) or rule.posix(), rule, key)


@functools.lru_cache(maxsize=4)
def _posix_host_zone(tz_env: str, localtime: str, day: date) -> PcZone:
    if tz_env:
        name = tz_env.lstrip(":")
        try:
            zone = zone_from_iana(name)
        except PcZoneError:
            zone = _from_posix(name)
    elif localtime:
        try:
            data = Path(localtime).read_bytes()
        except OSError as exc:
            raise PcZoneError("no se ha podido leer la zona horaria de este PC") from exc
        real = os.path.realpath(localtime).replace("\\", "/")
        zone = _from_posix(tzif_footer(data) or "", real.split("zoneinfo/", 1)[1] if "zoneinfo/" in real else "")
    else:
        zone = PcZone("UTC0", PosixZone(timedelta(0)), "UTC")      # sin /etc/localtime, el sistema va en UTC
    _check(zone.rule, day)
    return zone


def _check(rule: PosixZone, day: date) -> None:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if not agrees_with_this_pc(rule, start):
        raise PcZoneError("sus reglas de cambio de hora no se pueden expresar como una regla que se repite cada año")


def pc_zone(now: datetime | None = None) -> PcZone:
    """Zona horaria de este PC como zona POSIX comprobada (ver el docstring del módulo). Lanza `PcZoneError`."""
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    if sys.platform == "win32":
        tzi = _windows_tzi()
        if tzi is None:
            raise PcZoneError("Windows no ha dicho qué zona horaria usa este PC")
        return _windows_zone(tzi, day)
    localtime = "/etc/localtime" if os.path.exists("/etc/localtime") else ""
    return _posix_host_zone(os.environ.get("TZ", "").strip(), localtime, day)


__all__ = ["CHECK_DAYS", "PcZone", "PcZoneError", "PosixZone", "ZoneRule", "agrees_with_this_pc", "fmt_hms",
           "pc_zone", "posix_offset", "posix_zone", "tzdata_posix_for", "tzif_footer", "windows_zone_rule",
           "zone_from_iana"]
