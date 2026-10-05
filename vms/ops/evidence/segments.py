"""Segmentos fMP4 de MediaMTX que cubren un intervalo, incluidos los copiados a `evidence/protected/`.

MediaMTX graba `<grabaciones>/<cámara>/main/AAAA-MM-DD_HH-MM-SS-ffffff+HHMM.mp4`. Un segmento va desde su
inicio hasta el inicio del siguiente (como mucho `segment_seconds` + 1 min). Si la retención ya borró el
original pero el tramo estaba protegido, se usa la copia de `evidence/protected/<cámara>/<marcador>/`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from vms.core.naming import is_valid_id

SEGMENT_MARGIN_S = 60
# Mismo formato que el vigilante de disco (vms/engine/disk_guard.py, de B1); una prueba comprueba que los dos
# interpretan igual los nombres (tests/ops/test_evidence.py). Se repite aquí para no depender de vms.engine.
SEGMENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(\d{1,9})([+-]\d{4})?\.mp4$")


def parse_segment_name(name: str) -> datetime | None:
    """Inicio del segmento en UTC. Nombres antiguos sin desfase: se interpretan en la hora local del PC."""
    m = SEGMENT_RE.match(name)
    if not m:
        return None
    try:
        base = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
        frac = m.group(2)
        base = base.replace(microsecond=int((frac + "000000")[:6]))
        if m.group(3):
            base = base.replace(tzinfo=datetime.strptime(m.group(3), "%z").tzinfo)
        else:
            base = base.astimezone()
    except ValueError:
        return None
    return base.astimezone(timezone.utc)


@dataclass(frozen=True)
class SegmentRef:
    path: Path
    name: str
    start: datetime
    size: int
    source: Literal["recordings", "protected"]


def _scan(folder: Path, source: Literal["recordings", "protected"]) -> list[SegmentRef]:
    out: list[SegmentRef] = []
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
        out.append(SegmentRef(Path(e.path), e.name, start.astimezone(timezone.utc), size, source))
    return out


def all_segments(recordings_dir: Path, protected_dir: Path, camera_id: str) -> list[SegmentRef]:
    if not is_valid_id(camera_id):
        raise ValueError("Identificador de cámara no válido")
    by_name: dict[str, SegmentRef] = {s.name: s for s in _scan(Path(recordings_dir) / camera_id / "main",
                                                                "recordings")}
    prot_root = Path(protected_dir) / camera_id
    try:
        folders = [Path(e.path) for e in os.scandir(prot_root) if e.is_dir()]
    except FileNotFoundError:
        folders = []
    for folder in folders:
        for s in _scan(folder, "protected"):
            by_name.setdefault(s.name, s)
    return sorted(by_name.values(), key=lambda s: s.start)


def segments_in_range(recordings_dir: Path, protected_dir: Path, camera_id: str, start: datetime, end: datetime,
                      segment_seconds: int) -> list[SegmentRef]:
    segs = all_segments(recordings_dir, protected_dir, camera_id)
    max_len = timedelta(seconds=segment_seconds + SEGMENT_MARGIN_S)
    out = []
    for i, s in enumerate(segs):
        nxt = segs[i + 1].start if i + 1 < len(segs) else None
        seg_end = min(nxt, s.start + max_len) if nxt is not None else s.start + max_len
        if s.start < end and seg_end > start:
            out.append(s)
    return out
