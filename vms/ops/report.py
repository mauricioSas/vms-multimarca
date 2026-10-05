"""Informe de salud diario por tienda (CONTRATO §18.5) y su resumen para el latido.

Contenido por cámara: disponibilidad (tiempo con grabación sobre el tiempo transcurrido del día), huecos de
grabación (minutos sin segmento, desde los tramos del motor), puntuación de imagen mínima y sus causas,
desfase horario, días de retención reales y tramos protegidos. Además: disco (libre, previsión y SMART si
`vmsctl diag` lo ha dejado en `<datos>/ops/smart.json`) y la hora del PC.

`payload.health` del latido (aditivo a §7.3): `report_date`, `status`, `score_min`, `cameras_critical`,
`cameras_warning`, `clock_worst_s`, `forecast_days` y `problems` (máximo 10 frases, sin imágenes ni IP).
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vms.core.atomic import atomic_write_text
from vms.core.interfaces import RecordingSpan

from .models import HEALTH_CAUSE_ES, CameraDayReport, ClockCheck, HealthReport, RetentionForecast

log = logging.getLogger("vms.ops.report")

SUMMARY_FILE = "health-summary.json"
MIN_GAP_S = 60.0
MAX_PROBLEMS = 10


def site_tz(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = site_tz(tz_name)
    start = datetime.combine(day, time(0), tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def gaps(spans: list[RecordingSpan], start: datetime, end: datetime, min_gap_s: float = MIN_GAP_S) \
        -> list[tuple[datetime, datetime]]:
    """Huecos sin grabación dentro de [start, end) de al menos `min_gap_s`."""
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    for s in sorted(spans, key=lambda x: x.start):
        a, b = max(s.start, start), min(s.end, end)
        if b <= a:
            continue
        if (a - cursor).total_seconds() >= min_gap_s:
            out.append((cursor, a))
        cursor = max(cursor, b)
    if (end - cursor).total_seconds() >= min_gap_s:
        out.append((cursor, end))
    return out


@dataclass
class CameraInput:
    camera_id: str
    name: str
    record: bool
    online_now: bool | None
    spans: list[RecordingSpan] | None          # None = el motor no respondió
    score_min: int | None
    causes: list[str]
    clock: ClockCheck | None
    days_on_disk: float | None
    protected: int


def build(site_id: str, day: date, tz_name: str, cams: list[CameraInput], *, now: datetime,
          forecast: RetentionForecast | None, disk: dict[str, float], pc_clock: ClockCheck | None,
          protected_oldest_days: float | None = None) -> HealthReport:
    start, end = day_bounds(day, tz_name)
    upto = min(end, now)
    elapsed = max(0.0, (upto - start).total_seconds())
    rows: list[CameraDayReport] = []
    crit: list[str] = []
    warn: list[str] = []
    for c in cams:
        gap_min = 0.0
        online = 1.0 if c.online_now else 0.0
        if c.record and c.spans is not None and elapsed > 0:
            gs = gaps(c.spans, start, upto)
            gap_s = sum((b - a).total_seconds() for a, b in gs)
            gap_min = round(gap_s / 60, 1)
            online = round(max(0.0, 1 - gap_s / elapsed), 4)
        rows.append(CameraDayReport(camera_id=c.camera_id, name=c.name, online_ratio=online,
                                    recording_gaps_min=gap_min, health_score_min=c.score_min,
                                    health_causes=[x for x in c.causes if x in HEALTH_CAUSE_ES],
                                    clock_skew_s=c.clock.skew_s if c.clock else None,
                                    retention_days_real=c.days_on_disk, protected_ranges=c.protected))
        if c.record and c.spans is not None and gap_min >= 60:
            crit.append(f"{c.name}: {_mins(gap_min)} sin grabar")
        elif c.record and c.spans is not None and gap_min >= 5:
            warn.append(f"{c.name}: {_mins(gap_min)} sin grabar")
        if c.online_now is False:
            crit.append(f"{c.name}: sin vídeo ahora mismo")
        if c.score_min is not None and c.score_min < 50:
            crit.append(f"{c.name}: {_causes(c.causes) or 'imagen con problemas'} (puntuación {c.score_min})")
        elif c.score_min is not None and c.score_min < 80:
            warn.append(f"{c.name}: {_causes(c.causes) or 'imagen mejorable'} (puntuación {c.score_min})")
        if c.clock and c.clock.status == "critical":
            crit.append(f"{c.name}: hora desajustada {c.clock.skew_s:+.0f} s" if c.clock.skew_s is not None
                        else f"{c.name}: hora desajustada")
        elif c.clock and c.clock.status == "warning":
            warn.append(f"{c.name}: {c.clock.message_es}")
    if forecast is not None and forecast.status != "ok":
        (crit if forecast.status == "critical" else warn).append(
            f"Previsión de grabación: {forecast.forecast_days:.0f} días frente a un objetivo de {forecast.target_days}")
    if disk.get("smart_ok") == 0:
        crit.append("El disco de grabación avisa de fallos (SMART): cámbialo pronto")
    if pc_clock is not None and pc_clock.status in ("warning", "critical"):
        (crit if pc_clock.status == "critical" else warn).append(f"Hora del PC: {pc_clock.message_es}")
    if protected_oldest_days is not None:
        n = sum(r.protected_ranges for r in rows)
        if n:
            warn_text = f"{n} tramos protegidos, el más antiguo de hace {protected_oldest_days:.0f} días"
            if protected_oldest_days > 90:
                warn.append(warn_text + ": revisa si siguen haciendo falta")
    status = "critical" if crit else "warning" if warn else "ok"
    return HealthReport(site_id=site_id, date=day.isoformat(), generated_at=now, status=status,
                        problems=crit + warn, cameras=rows, disk=disk, retention=forecast, pc_clock=pc_clock)


def _mins(m: float) -> str:
    return f"{m / 60:.1f} h".replace(".", ",") if m >= 120 else f"{m:.0f} min"


def _causes(causes: list[str]) -> str:
    return ", ".join(HEALTH_CAUSE_ES.get(c, c).lower() for c in causes[:2])


def heartbeat_summary(report: HealthReport) -> dict[str, Any]:
    scores = [c.health_score_min for c in report.cameras if c.health_score_min is not None]
    skews = [abs(c.clock_skew_s) for c in report.cameras if c.clock_skew_s is not None]
    crit = sum(1 for c in report.cameras if (c.health_score_min is not None and c.health_score_min < 50)
               or c.recording_gaps_min >= 60)
    warn = sum(1 for c in report.cameras if (c.health_score_min is not None and 50 <= c.health_score_min < 80)
               or 5 <= c.recording_gaps_min < 60)
    return {"report_date": report.date, "status": report.status, "score_min": min(scores) if scores else None,
            "cameras_critical": crit, "cameras_warning": warn,
            "clock_worst_s": round(max(skews), 1) if skews else None,
            "forecast_days": report.retention.forecast_days if report.retention else None,
            "problems": [p[:200] for p in report.problems[:MAX_PROBLEMS]]}


def write_summary(ops_dir: Path, report: HealthReport) -> dict[str, Any]:
    data = heartbeat_summary(report)
    ops_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ops_dir / SUMMARY_FILE, json.dumps(data, ensure_ascii=False))
    return data


def read_summary(ops_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((ops_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_smart(ops_dir: Path) -> dict[str, float]:
    """`smart.json` que deja `vmsctl diag` (B1) con Get-PhysicalDisk/Get-StorageReliabilityCounter."""
    try:
        data = json.loads((ops_dir / "smart.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or "healthy" not in data:
        return {}
    return {"smart_ok": 1.0 if data.get("healthy") else 0.0}


def _num(v: float | int | None, digits: int = 1) -> str:
    if v is None:
        return ""
    return f"{v:.{digits}f}".replace(".", ",")


def report_csv(report: HealthReport) -> bytes:
    """CSV para Excel en español: `;`, coma decimal y UTF-8 con BOM."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["tienda", "fecha", "camara", "disponibilidad_pct", "minutos_sin_grabar", "puntuacion_min", "causas",
                "desfase_s", "dias_grabados", "tramos_protegidos"])
    for c in report.cameras:
        w.writerow([report.site_id, report.date, c.name, _num(c.online_ratio * 100), _num(c.recording_gaps_min),
                    "" if c.health_score_min is None else c.health_score_min,
                    ", ".join(HEALTH_CAUSE_ES.get(str(x), str(x)) for x in c.health_causes), _num(c.clock_skew_s),
                    _num(c.retention_days_real), c.protected_ranges])
    return ("﻿" + buf.getvalue()).encode("utf-8")
