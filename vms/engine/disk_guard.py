"""Vigilante de disco: borra las grabaciones más antiguas si el volumen supera el umbral.

MediaMTX ya borra por antigüedad (recordDeleteAfter = días de retención). Esto cubre el otro
caso: el disco se llena antes de tiempo (más cámaras, más bitrate, disco pequeño).

Seguridad:
- Solo se tocan archivos con el patrón exacto de MediaMTX
  <grabaciones>/<id_cámara>/<main|sub>/AAAA-MM-DD_HH-MM-SS-ffffff+HHMM.mp4 (o el formato antiguo,
  sin desfase horario, de las grabaciones hechas antes de la versión que lo añadió)
- Nunca se borra el segmento más reciente de cada ruta ni uno modificado hace menos de
  `protect_seconds` (el que se está escribiendo).
- Se borra del más antiguo al más nuevo, mezclando todas las cámaras.
- Si ni borrando todo lo antiguo se bajaría del umbral (el disco lo llena otra cosa), no se
  borra nada: solo se avisa en el registro y en el estado del motor.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from vms.core.naming import is_valid_id

log = logging.getLogger("vms.engine.disk_guard")

SEGMENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(\d{1,9})([+-]\d{4})?\.mp4$")


@dataclass
class Segment:
    path: Path
    route: str            # «cam-xxxx/main»
    start: datetime       # inicio en UTC (con zona), para ordenar
    size: int
    mtime: float


@dataclass
class GuardResult:
    deleted: list[Path] = field(default_factory=list)
    freed_bytes: int = 0
    percent_before: float = 0.0
    percent_after: float = 0.0
    target_reached: bool = True


def parse_segment_name(name: str) -> datetime | None:
    """Inicio del segmento en UTC. Nombres antiguos sin desfase: se interpretan en la hora local."""
    m = SEGMENT_RE.match(name)
    if not m:
        return None
    try:
        base = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
        if m.group(3):
            base = base.replace(tzinfo=datetime.strptime(m.group(3), "%z").tzinfo)
        else:
            base = base.astimezone()  # hora local del equipo (formato antiguo)
    except ValueError:
        return None
    micro = int(m.group(2)[:6].ljust(6, "0"))
    return base.replace(microsecond=micro).astimezone(timezone.utc)


def _offset_suffix(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}{total % 60:02d}"


def legacy_offset(local_naive: datetime, mtime: float) -> str:
    """Desfase («+0200») que corresponde a la hora local `local_naive` de un nombre antiguo.

    En la hora repetida del cambio de hora de octubre hay dos candidatas; se elige la que deja el
    inicio antes de la última escritura del archivo (`mtime`) y más cerca de ella."""
    cands = []
    for fold in (0, 1):
        aware = local_naive.replace(fold=fold).astimezone()
        cands.append((aware.timestamp(), aware))
    valid = [c for c in cands if c[0] <= mtime + 1]
    _ts, best = max(valid) if valid else cands[0]
    return _offset_suffix(best)


def migrate_legacy_names(root: Path) -> int:
    """Renombra grabaciones antiguas (sin desfase horario) al formato nuevo para que MediaMTX las
    siga listando y borrando. Devuelve cuántas se renombraron. Solo con MediaMTX parado."""
    root = Path(root)
    renamed = 0
    try:
        cams = [e for e in os.scandir(root) if e.is_dir() and is_valid_id(e.name)]
    except OSError:
        return 0
    for cam in cams:
        for stream in ("main", "sub"):
            d = Path(cam.path) / stream
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for f in entries:
                m = SEGMENT_RE.match(f.name)
                if not m or m.group(3):
                    continue
                try:
                    local = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
                    target = d / f"{m.group(1)}-{m.group(2)}{legacy_offset(local, f.stat().st_mtime)}.mp4"
                    if target.exists():
                        continue
                    os.replace(f.path, target)
                    renamed += 1
                except (OSError, ValueError) as exc:
                    log.warning("No se pudo renombrar la grabación %s: %s", f.name, exc)
    if renamed:
        log.info("Se renombraron %d grabaciones antiguas al formato con desfase horario", renamed)
    return renamed


def scan(root: Path) -> list[Segment]:
    out: list[Segment] = []
    try:
        cams = [e for e in os.scandir(root) if e.is_dir() and is_valid_id(e.name)]
    except OSError:
        return out
    for cam in cams:
        for stream in ("main", "sub"):
            d = Path(cam.path) / stream
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for f in entries:
                start = parse_segment_name(f.name)
                if start is None:
                    continue
                try:
                    if not f.is_file():
                        continue
                    st = f.stat()
                except OSError:
                    continue
                out.append(Segment(Path(f.path), f"{cam.name}/{stream}", start, st.st_size, st.st_mtime))
    out.sort(key=lambda s: (s.start, s.route))
    return out


def plan(segments: list[Segment], total: int, used: int, max_percent: float, now_ts: float,
         protect_seconds: float = 120.0) -> tuple[list[Segment], bool]:
    """Qué borrar para quedar por debajo de `max_percent`. Devuelve (víctimas, objetivo_alcanzado)."""
    if total <= 0 or max_percent <= 0:
        return [], True
    target = total * max_percent / 100.0
    if used <= target:
        return [], True
    newest: dict[str, Segment] = {}
    for s in segments:
        if s.route not in newest or s.start > newest[s.route].start:
            newest[s.route] = s
    candidates = [s for s in segments if newest.get(s.route) is not s and (now_ts - s.mtime) >= protect_seconds]
    if used - sum(s.size for s in candidates) > target:
        # Ni borrando todas las grabaciones se bajaría del umbral: el disco lo llena otra cosa.
        # No se destruye el histórico para nada; solo se avisa.
        return [], False
    victims: list[Segment] = []
    for s in candidates:
        if used <= target:
            break
        victims.append(s)
        used -= s.size
    return victims, used <= target


def disk_usage(path: Path) -> tuple[int, int, int]:
    """(total, usado, libre) del volumen que contiene `path` (o su primer ancestro existente)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    u = shutil.disk_usage(p)
    return u.total, u.used, u.free


def run(root: Path, max_percent: float, *, now_ts: float | None = None, protect_seconds: float = 120.0,
        usage_fn: Callable[[Path], tuple[int, int, int]] = disk_usage) -> GuardResult:
    import time

    root = Path(root)
    result = GuardResult()
    if max_percent <= 0 or not root.exists():
        return result
    total, used, _ = usage_fn(root)
    result.percent_before = result.percent_after = (used / total * 100.0) if total else 0.0
    if not total or used <= total * max_percent / 100.0:
        return result
    victims, reached = plan(scan(root), total, used, max_percent, now_ts if now_ts is not None else time.time(),
                            protect_seconds)
    for seg in victims:
        try:
            seg.path.unlink()
        except FileNotFoundError:
            continue  # MediaMTX lo borró por antigüedad mientras tanto
        except OSError as exc:
            log.warning("No se pudo borrar la grabación %s: %s", seg.path.name, exc)
            continue
        result.deleted.append(seg.path)
        result.freed_bytes += seg.size
    total, used, _ = usage_fn(root)
    result.percent_after = (used / total * 100.0) if total else 0.0
    result.target_reached = reached and used <= total * max_percent / 100.0
    if result.deleted:
        log.warning("Disco de grabación al %.1f %% (umbral %s %%): se borraron %d segmentos antiguos "
                    "(%.1f MB). Ahora al %.1f %%.", result.percent_before, max_percent, len(result.deleted),
                    result.freed_bytes / 1e6, result.percent_after)
    if not result.target_reached:
        log.error("El disco de grabación sigue al %.1f %% (umbral %s %%) y borrar grabaciones antiguas no "
                  "basta para bajar del umbral. Libera espacio, reduce los días de retención o usa un disco mayor.",
                  result.percent_after, max_percent)
    return result
