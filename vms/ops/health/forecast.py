"""Previsión de días de grabación (CONTRATO §18.4).

Tasa real por cámara = bytes por hora de los segmentos de las últimas 24 h en `<grabaciones>/<cámara>/main/`
(nombres de MediaMTX: `AAAA-MM-DD_HH-MM-SS-ffffff+HHMM.mp4`). Capacidad para grabaciones = lo que ya
ocupan + lo libre − el margen que deja el vigilante de disco (`disk_guard_percent`).

    días previstos = capacidad / (suma de tasas × 24)

Aviso si los días previstos no llegan al objetivo (`settings.retention.days`); grave si no llegan a la
mitad. Aviso RGPD si el objetivo pasa de 30 días (art. 22.3 LOPDGDD). `simulate` responde a «¿y si añado
4 cámaras de 4 Mbit/s?» con la misma cuenta.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from vms.core.naming import is_valid_id

from ..evidence.segments import parse_segment_name
from ..models import CameraForecast, RetentionForecast

RGPD_MAX_DAYS = 30
WINDOW_H = 24.0


class DiskStats(NamedTuple):
    total: int
    used: int
    free: int


DiskUsageFn = Callable[[str], DiskStats]


def _disk_usage(path: str) -> DiskStats:
    u = shutil.disk_usage(path)
    return DiskStats(u.total, u.used, u.free)


@dataclass
class SegmentInfo:
    path: Path
    start: datetime
    size: int


def camera_segments(recordings_dir: Path, camera_id: str, stream: str = "main") -> list[SegmentInfo]:
    """Segmentos de MediaMTX de una cámara, ordenados por inicio (solo nombres con el formato exacto)."""
    folder = Path(recordings_dir) / camera_id / stream
    out: list[SegmentInfo] = []
    try:
        entries = list(os.scandir(folder))
    except (FileNotFoundError, NotADirectoryError):
        return out
    for e in entries:
        if not e.is_file():
            continue
        start = parse_segment_name(e.name)
        if start is None:
            continue
        try:
            size = e.stat().st_size
        except OSError:
            continue
        out.append(SegmentInfo(Path(e.path), start.astimezone(timezone.utc), size))
    out.sort(key=lambda s: s.start)
    return out


def _camera_rate(segs: list[SegmentInfo], now: datetime) -> tuple[float, float, int]:
    """(bytes/hora en las últimas 24 h, días grabados, bytes totales)."""
    total = sum(s.size for s in segs)
    if not segs:
        return 0.0, 0.0, 0
    days = max(0.0, (now - segs[0].start).total_seconds() / 86400)
    window_start = now - timedelta(hours=WINDOW_H)
    recent = [s for s in segs if s.start >= window_start]
    if not recent:
        return 0.0, days, total
    hours = max(0.25, (now - min(s.start for s in recent)).total_seconds() / 3600)
    return sum(s.size for s in recent) / hours, days, total


def forecast(recordings_dir: str | Path, camera_ids: list[str], target_days: int, guard_percent: int, *,
             now: datetime | None = None, disk_usage: DiskUsageFn = _disk_usage,
             extra_bytes_per_hour: float = 0.0) -> RetentionForecast:
    now = now or datetime.now(timezone.utc)
    root = Path(recordings_dir)
    disk = disk_usage(str(root if root.exists() else root.parent))
    cams: list[CameraForecast] = []
    rate_total = 0.0
    rec_bytes = 0
    reclaimable = 0
    cutoff = now - timedelta(days=target_days)
    for cid in camera_ids:
        if not is_valid_id(cid):
            continue
        segs = camera_segments(root, cid)
        rate, days, total = _camera_rate(segs, now)
        rate_total += rate
        rec_bytes += total
        reclaimable += sum(s.size for s in segs if s.start < cutoff)
        cams.append(CameraForecast(camera_id=cid, bytes_per_hour=round(rate, 1), days_on_disk=round(days, 2)))
    rate_total += max(0.0, extra_bytes_per_hour)
    reserve = disk.total * (100 - guard_percent) / 100 if guard_percent else 0
    capacity = max(0.0, rec_bytes + disk.free - reserve)
    forecast_days = capacity / (rate_total * 24) if rate_total > 0 else float("inf")
    status = "ok"
    if rate_total > 0 and forecast_days < target_days:
        status = "critical" if forecast_days < target_days / 2 else "warning"
    rgpd = target_days > RGPD_MAX_DAYS
    if rate_total <= 0:
        msg = "Todavía no hay grabaciones de las últimas 24 horas para calcular la previsión."
    elif status == "ok":
        msg = (f"Con el disco actual caben unos {_days(forecast_days)} de grabación; el objetivo es "
               f"{target_days} días.")
    else:
        msg = (f"Solo caben unos {_days(forecast_days)} de grabación y el objetivo es {target_days} días: "
               "el vigilante de disco borrará antes de tiempo. Amplía el disco, baja la calidad del flujo "
               "principal o reduce los días.")
    if rgpd:
        msg += (" Ojo: conservar más de 30 días solo está permitido para acreditar un incidente concreto "
                "(art. 22.3 LOPDGDD).")
    return RetentionForecast(at=now, target_days=target_days,
                             forecast_days=round(forecast_days, 1) if forecast_days != float("inf") else 9999.0,
                             disk_total=disk.total, disk_free=disk.free, reclaimable=reclaimable, status=status,
                             rgpd_warning=rgpd, cameras=cams, message_es=msg)


def _days(d: float) -> str:
    if d >= 9999:
        return "muchos días"
    return f"{d:.0f} días" if d >= 2 else f"{d:.1f} días".replace(".", ",")


def mbps_to_bytes_per_hour(mbps: float) -> float:
    return mbps * 1_000_000 / 8 * 3600
