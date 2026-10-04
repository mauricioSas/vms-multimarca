"""Retención: borra lo más antiguo por días y/o por porcentaje de disco ocupado.

Seguridad: solo se borran archivos con el patrón exacto de segmento
(<cámara>/<AAAA-MM-DD>/<AAAA-MM-DD_HH-MM-SS>.mkv) dentro de la carpeta de grabación.
Nunca se borra el segmento que se está escribiendo (modificado hace poco) ni el más
reciente de cada cámara.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .segments import DAY_RE, parse_start

log = logging.getLogger(__name__)


@dataclass
class SegmentFile:
    path: Path
    camera: str
    start: datetime
    size: int
    mtime: float


@dataclass
class RetentionResult:
    deleted: list[Path] = field(default_factory=list)
    freed_bytes: int = 0
    by_days: int = 0
    by_disk: int = 0
    target_reached: bool = True


def scan(root: Path) -> list[SegmentFile]:
    root = Path(root)
    out: list[SegmentFile] = []
    try:
        cams = [e for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return out
    for cam in cams:
        try:
            days = [e for e in os.scandir(cam.path) if e.is_dir() and DAY_RE.match(e.name)]
        except OSError:
            continue
        for day in days:
            try:
                files = list(os.scandir(day.path))
            except OSError:
                continue
            for f in files:
                start = parse_start(f.name)
                if start is None or not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                out.append(SegmentFile(Path(f.path), cam.name, start, st.st_size, st.st_mtime))
    out.sort(key=lambda s: s.start)
    return out


def plan(segments: list[SegmentFile], now: datetime, max_days: int, max_disk_percent: int,
         disk_total: int, disk_used: int, protect_seconds: int = 120) -> tuple[list[tuple[SegmentFile, str]], bool]:
    """Decide qué borrar. Devuelve ([(segmento, motivo)], objetivo_de_disco_alcanzado)."""
    newest: dict[str, SegmentFile] = {}
    for s in segments:
        if s.camera not in newest or s.start > newest[s.camera].start:
            newest[s.camera] = s
    now_ts = now.timestamp()

    def protected(s: SegmentFile) -> bool:
        return newest.get(s.camera) is s or (now_ts - s.mtime) < protect_seconds

    to_delete: list[tuple[SegmentFile, str]] = []
    chosen = set()
    if max_days and max_days > 0:
        cutoff = now - timedelta(days=max_days)
        for s in segments:
            if s.start < cutoff and not protected(s):
                to_delete.append((s, "días"))
                chosen.add(id(s))

    reached = True
    if max_disk_percent and max_disk_percent > 0 and disk_total > 0:
        used = disk_used - sum(s.size for s, _ in to_delete)
        target = disk_total * max_disk_percent / 100.0
        if used > target:
            for s in segments:  # ya ordenados del más antiguo al más nuevo
                if used <= target:
                    break
                if id(s) in chosen or protected(s):
                    continue
                to_delete.append((s, "disco"))
                chosen.add(id(s))
                used -= s.size
            reached = used <= target
    return to_delete, reached


def apply(root: Path, max_days: int, max_disk_percent: int, now: datetime | None = None,
          usage_fn: Callable[[Path], tuple[int, int]] | None = None,
          protect_seconds: int = 120) -> RetentionResult:
    root = Path(root)
    result = RetentionResult()
    if not root.exists():
        return result
    now = now or datetime.now()
    if usage_fn is None:
        def usage_fn(p: Path) -> tuple[int, int]:
            u = shutil.disk_usage(p)
            return u.total, u.used
    total, used = usage_fn(root)
    segments = scan(root)
    victims, reached = plan(segments, now, max_days, max_disk_percent, total, used, protect_seconds)
    result.target_reached = reached
    touched_dirs = set()
    for seg, reason in victims:
        try:
            seg.path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("No se pudo borrar %s: %s", seg.path, exc)
            continue
        result.deleted.append(seg.path)
        result.freed_bytes += seg.size
        if reason == "días":
            result.by_days += 1
        else:
            result.by_disk += 1
        touched_dirs.add(seg.path.parent)
    for d in touched_dirs:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    if result.deleted:
        log.info("Retención: %d segmentos borrados (%d por días, %d por disco), %.1f MB liberados",
                 len(result.deleted), result.by_days, result.by_disk, result.freed_bytes / 1e6)
    if not reached:
        log.warning("Retención: no se alcanzó el límite de disco solo con grabaciones; "
                    "hay otros datos ocupando el disco de grabación")
    return result
