"""Nombres de los segmentos grabados y localización por fecha y hora.

Estructura en disco:
    <carpeta de grabación>/<carpeta de la cámara>/<AAAA-MM-DD>/<AAAA-MM-DD_HH-MM-SS>.mkv
El nombre del archivo es la hora local de inicio del segmento. El fin se estima con el
inicio del segmento siguiente o con la fecha de modificación del archivo (cuando ffmpeg
cierra un segmento, su fecha de modificación coincide con el final).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

SEGMENT_EXT = ".mkv"
DAY_FMT = "%Y-%m-%d"
FILE_FMT = "%Y-%m-%d_%H-%M-%S"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.mkv$")
GAP_TOLERANCE = timedelta(seconds=3)


@dataclass(frozen=True)
class Segment:
    path: Path
    start: datetime
    end: datetime

    @property
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()


def ffmpeg_output_pattern(camera_dir: Path) -> str:
    """Patrón strftime para el muxer segment de ffmpeg. Los % de la ruta base se escapan."""
    base = str(camera_dir).replace("%", "%%")
    return os.path.join(base, "%Y-%m-%d", "%Y-%m-%d_%H-%M-%S" + SEGMENT_EXT)


def parse_start(filename: str) -> datetime | None:
    m = FILE_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), FILE_FMT)
    except ValueError:
        return None


def day_dir(camera_dir: Path, day: date) -> Path:
    return Path(camera_dir) / day.strftime(DAY_FMT)


def _raw_segments(camera_dir: Path, days: list[date]) -> list[tuple[Path, datetime, datetime]]:
    out = []
    for d in days:
        folder = day_dir(camera_dir, d)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            start = parse_start(e.name)
            if start is None or not e.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(e.stat().st_mtime)
            except OSError:
                continue
            out.append((Path(e.path), start, mtime))
    out.sort(key=lambda t: t[1])
    return out


def build_segments(raw: list[tuple[Path, datetime, datetime]]) -> list[Segment]:
    """raw = [(ruta, inicio, mtime)] ordenado por inicio."""
    segs = []
    for i, (path, start, mtime) in enumerate(raw):
        end = mtime if mtime > start else start + timedelta(seconds=1)
        if i + 1 < len(raw) and raw[i + 1][1] < end:
            end = raw[i + 1][1]
        segs.append(Segment(path, start, end))
    return segs


def list_segments(camera_dir: Path, day: date) -> list[Segment]:
    """Segmentos de un día (incluye el último del día anterior si llega a este día)."""
    raw = _raw_segments(camera_dir, [day - timedelta(days=1), day, day + timedelta(days=1)])
    segs = build_segments(raw)
    lo = datetime.combine(day, datetime.min.time())
    hi = lo + timedelta(days=1)
    return [s for s in segs if s.end > lo and s.start < hi]


def list_days(camera_dir: Path) -> list[date]:
    days = []
    try:
        for e in os.scandir(camera_dir):
            if e.is_dir() and DAY_RE.match(e.name):
                try:
                    days.append(datetime.strptime(e.name, DAY_FMT).date())
                except ValueError:
                    pass
    except OSError:
        pass
    return sorted(days)


def locate(segments: list[Segment], when: datetime) -> tuple[Segment, float] | None:
    """Segmento que contiene «when» y desplazamiento en segundos dentro de él."""
    for s in segments:
        if s.start <= when < s.end + GAP_TOLERANCE:
            offset = max(0.0, min((when - s.start).total_seconds(), s.duration))
            return s, offset
    return None


def next_segment(segments: list[Segment], when: datetime) -> Segment | None:
    for s in segments:
        if s.start > when:
            return s
    return None


def prev_segment(segments: list[Segment], when: datetime) -> Segment | None:
    prev = None
    for s in segments:
        if s.start >= when:
            break
        prev = s
    return prev


def find_for_playback(camera_dir: Path, when: datetime) -> tuple[Segment, float, bool] | None:
    """(segmento, desplazamiento, exacto). Si no hay grabación a esa hora, devuelve el
    siguiente segmento disponible ese mismo día con exacto=False."""
    segs = list_segments(camera_dir, when.date())
    hit = locate(segs, when)
    if hit:
        return hit[0], hit[1], True
    nxt = next_segment(segs, when)
    if nxt:
        return nxt, 0.0, False
    return None
