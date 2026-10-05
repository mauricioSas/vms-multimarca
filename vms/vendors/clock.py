"""Utilidades de hora para la capacidad TIME_READ (CONTRATO §18.3): medida a mitad del viaje y zonas.

La hora del PC se toma antes y después de la petición y se usa el punto medio, así el desfase no incluye
el tiempo de red. Las zonas horarias de los equipos llegan como offset ISO («+02:00») o en estilo POSIX
(«CST-1:00:00», «CET-1CEST,M3.5.0,M10.5.0/3»), donde el signo va al revés que en ISO.

Semántica común a todas las marcas (CONTRATO §18.3): `DeviceTime.device_time` es el INSTANTE real del reloj del
equipo (con su zona), así el desfase se mide siempre en UTC; `DeviceTime.utc_offset_s` es la zona que usa el
equipo para la hora que sobreimprime en la imagen. Si la hora local del equipo llega sin zona y su regla de
horario de verano no se entiende, no se adivina: la lectura falla («Desconocido»).
"""
from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

_POSIX_RE = re.compile(r"^[A-Za-z]{3,}([+-]?)(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?")
_NAME = r"(?:[A-Za-z]{3,}|<[^>]+>)"
_OFF = r"[+-]?\d{1,2}(?::\d{2}){0,2}"
_FULL_RE = re.compile(rf"^(?P<std>{_NAME})(?P<stdoff>{_OFF})(?:(?P<dst>{_NAME})(?P<dstoff>{_OFF})?"
                      r"(?:,(?P<start>[^,]+),(?P<end>[^,]+))?)?$")
_RULE_RE = re.compile(r"^M(\d{1,2})\.(\d)\.(\d)(?:/([+-]?\d{1,3}(?::\d{2}){0,2}))?$")
_LOOSE_DT_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:\.\d+)?\s*$")


class Stopwatch:
    """`with Stopwatch() as sw: …` → sw.midpoint (UTC) y sw.round_trip_ms."""

    def __enter__(self) -> Stopwatch:
        self._t0 = time.monotonic()
        self._wall0 = datetime.now(timezone.utc)
        return self

    def __exit__(self, *exc: object) -> None:
        self.round_trip_ms = (time.monotonic() - self._t0) * 1000.0
        self.midpoint = self._wall0 + timedelta(milliseconds=self.round_trip_ms / 2)


def _hms(text: str) -> timedelta:
    sign = -1 if text.startswith("-") else 1
    parts = [int(p) for p in text.lstrip("+-").split(":")]
    parts += [0] * (3 - len(parts))
    return sign * timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2])


def posix_offset(tz: str) -> timezone | None:
    """«CST-1:00:00» → UTC+1 (POSIX invierte el signo). None si no se entiende. Solo la parte estándar."""
    m = _POSIX_RE.match(tz.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return timezone(timedelta(minutes=-sign * minutes))


@dataclass(frozen=True)
class _Rule:
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


def _rule(text: str) -> _Rule | None:
    m = _RULE_RE.match(text.strip())
    if not m:
        return None   # formas «Jn» y «n» (día juliano): no se usan en estos equipos; no se adivinan
    month, week, wd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= week <= 5 and 0 <= wd <= 6):
        return None
    return _Rule(month, week, wd, _hms(m.group(4)) if m.group(4) else timedelta(hours=2))


@dataclass(frozen=True)
class PosixZone:
    """Zona POSIX con su regla de horario de verano (offsets en sentido ISO: UTC+1 = +1 h)."""

    std: timedelta
    dst: timedelta | None = None
    start: _Rule | None = None
    end: _Rule | None = None

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


def parse_device_datetime(text: str, fallback_tz: timezone | None = None) -> datetime | None:
    """ISO 8601 con o sin zona («2026-10-05T10:00:00+02:00», «2026-10-05 10:00:00», «…Z») y también la forma
    sin ceros del ejemplo de la API de Dahua («2011-7-3 21:02:32»)."""
    dt = parse_naive_or_aware(text)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=fallback_tz or local_tz())
    return dt


def parse_naive_or_aware(text: str) -> datetime | None:
    """Como `parse_device_datetime`, pero sin poner zona si no la trae."""
    value = (text or "").strip().replace("Z", "+00:00")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" ", "T", 1) if "T" not in value else value)
    except ValueError:
        pass
    m = _LOOSE_DT_RE.match(value)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def local_tz() -> timezone:
    """Zona del PC (los equipos de la tienda suelen estar en la misma)."""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return timezone(offset)


def pc_offset_at(instant: datetime) -> timedelta:
    """Offset de la zona del PC en un instante (tiene en cuenta el horario de verano del PC)."""
    return instant.astimezone().utcoffset() or timedelta(0)


def round_offset(delta: timedelta) -> int:
    """Offset en segundos redondeado al cuarto de hora (las zonas reales van de 15 en 15 min)."""
    return int(round(delta.total_seconds() / 900.0) * 900)


__all__ = ["PosixZone", "Stopwatch", "local_tz", "parse_device_datetime", "parse_naive_or_aware", "pc_offset_at",
           "posix_offset", "posix_zone", "round_offset"]
