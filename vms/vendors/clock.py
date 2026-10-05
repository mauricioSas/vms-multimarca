"""Utilidades de hora para la capacidad TIME_READ (CONTRATO §18.3): medida a mitad del viaje y zonas.

La hora del PC se toma antes y después de la petición y se usa el punto medio, así el desfase no incluye
el tiempo de red. Las zonas horarias de los equipos llegan como offset ISO («+02:00») o en estilo POSIX
(«CST-1:00:00», «CET-1CEST,M3.5.0,M10.5.0/3»), donde el signo va al revés que en ISO.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

_POSIX_RE = re.compile(r"^[A-Za-z]{3,}([+-]?)(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?")


class Stopwatch:
    """`with Stopwatch() as sw: …` → sw.midpoint (UTC) y sw.round_trip_ms."""

    def __enter__(self) -> Stopwatch:
        self._t0 = time.monotonic()
        self._wall0 = datetime.now(timezone.utc)
        return self

    def __exit__(self, *exc: object) -> None:
        self.round_trip_ms = (time.monotonic() - self._t0) * 1000.0
        self.midpoint = self._wall0 + timedelta(milliseconds=self.round_trip_ms / 2)


def posix_offset(tz: str) -> timezone | None:
    """«CST-1:00:00» → UTC+1 (POSIX invierte el signo). None si no se entiende."""
    m = _POSIX_RE.match(tz.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return timezone(timedelta(minutes=-sign * minutes))


def parse_device_datetime(text: str, fallback_tz: timezone | None = None) -> datetime | None:
    """ISO 8601 con o sin zona («2026-10-05T10:00:00+02:00», «2026-10-05 10:00:00», «…Z»)."""
    value = text.strip().replace("Z", "+00:00")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace(" ", "T", 1) if "T" not in value else value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=fallback_tz or local_tz())
    return dt


def local_tz() -> timezone:
    """Zona del PC (los equipos de la tienda suelen estar en la misma)."""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return timezone(offset)


__all__ = ["Stopwatch", "local_tz", "parse_device_datetime", "posix_offset"]
