"""Utilidades de hora para TIME_READ y TIME_WRITE (CONTRATO §18.3): medida a mitad del viaje y zonas.

La hora del PC se toma antes y después de la petición y se usa el punto medio, así el desfase no incluye
el tiempo de red. Las zonas horarias de los equipos llegan como offset ISO («+02:00») o en estilo POSIX
(«CST-1:00:00», «CET-1CEST,M3.5.0,M10.5.0/3»), donde el signo va al revés que en ISO; el analizador está en
`vms.core.tzposix` y se reexporta aquí.

Semántica común a todas las marcas (CONTRATO §18.3): `DeviceTime.device_time` es el INSTANTE real del reloj del
equipo (con su zona), así el desfase se mide siempre en UTC; `DeviceTime.utc_offset_s` es la zona que usa el
equipo para la hora que sobreimprime en la imagen. Si la hora local del equipo llega sin zona y su regla de
horario de verano no se entiende, no se adivina: la lectura falla («Desconocido»).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

# las zonas POSIX viven en vms.core (también las usa B6, que no importa los drivers); se reexportan aquí
from vms.core.tzposix import PosixZone, posix_offset, posix_zone

_LOOSE_DT_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:\.\d+)?\s*$")


def utc_now() -> datetime:
    """Hora UTC de este PC. Una sola función para la hora que se escribe y la que se mide (las pruebas la mueven para
    simular verano e invierno sin cambiar el reloj de verdad)."""
    return datetime.now(timezone.utc)


class Stopwatch:
    """`with Stopwatch() as sw: …` → sw.midpoint (UTC) y sw.round_trip_ms."""

    def __enter__(self) -> Stopwatch:
        self._t0 = time.monotonic()
        self._wall0 = utc_now()
        return self

    def __exit__(self, *exc: object) -> None:
        self.round_trip_ms = (time.monotonic() - self._t0) * 1000.0
        self.midpoint = self._wall0 + timedelta(milliseconds=self.round_trip_ms / 2)


def zone_matches(wanted: PosixZone, reported: str, offset_s: int | None, at: datetime) -> bool:
    """Comprobación de «Usar también la zona horaria de este PC» al leer de nuevo: ¿el equipo usa ya la zona pedida?

    `reported` es la zona tal como la da el equipo (Hikvision `timeZone`, ONVIF `TZ`): se comparan sus reglas, no el
    texto (el firmware puede escribirla a su manera). `offset_s` es el desfase de su hora local, si lo da, y tiene que
    ser el de la zona pedida en `at` (el instante de su reloj). Sin ninguno de los dos datos no se da por buena."""
    got = posix_zone(reported) if reported.strip() else None
    if reported.strip() and got != wanted:
        return False
    expected = wanted.offset_at_utc(at)
    if offset_s is not None and expected is not None and abs(offset_s - expected.total_seconds()) >= 60:
        return False
    return got is not None or offset_s is not None


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
    """Offset de la zona del PC en un instante (tiene en cuenta el horario de verano del PC).

    Un equipo sin hora (1970, año 2000…) da instantes que Windows no sabe pasar a hora local (`localtime` falla
    con fechas antes de 1970 o fuera de rango): entonces se usa la zona actual del PC."""
    try:
        return instant.astimezone().utcoffset() or timedelta(0)
    except (OSError, OverflowError, ValueError):
        return datetime.now().astimezone().utcoffset() or timedelta(0)


def round_offset(delta: timedelta) -> int:
    """Offset en segundos redondeado al cuarto de hora (las zonas reales van de 15 en 15 min)."""
    return int(round(delta.total_seconds() / 900.0) * 900)


__all__ = ["PosixZone", "Stopwatch", "local_tz", "parse_device_datetime", "parse_naive_or_aware", "pc_offset_at",
           "posix_offset", "posix_zone", "round_offset", "utc_now", "zone_matches"]
