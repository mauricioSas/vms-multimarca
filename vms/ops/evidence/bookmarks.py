"""Marcadores y bloqueo de retención (CONTRATO §18.6).

«Proteger» crea **enlaces duros** de los segmentos del tramo en
`<datos>/evidence/protected/<camera_id>/<bookmark_id>/` (o una copia si las grabaciones están en otro
volumen o el sistema no permite el enlace), con un `meta.json` (motivo, caso, caducidad, usuario). La
retención de MediaMTX borra el archivo original, pero el enlace sobrevive: el tramo se puede seguir
exportando. Al caducar (90 días por defecto) se borra la copia y queda anotado.

Así se cumple el art. 22.3 LOPDGDD: se conserva más tiempo **solo** lo que acredita un hecho, con motivo.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vms.core.atomic import atomic_write_text
from vms.core.errors import ValidationFailed

from ..models import Bookmark, BookmarkCreate, TimelineEvent
from ..store import OpsStore
from .segments import segments_in_range

log = logging.getLogger("vms.ops.bookmarks")

MAX_PROTECT_SPAN = timedelta(hours=24)


def new_bookmark_id() -> str:
    return f"bm-{secrets.token_hex(4)}"


def link_or_copy(src: Path, dst: Path) -> str:
    """Enlace duro; si no se puede (otro volumen, sistema de archivos sin enlaces), copia. Devuelve el modo."""
    try:
        os.link(src, dst)
        return "link"
    except OSError as exc:   # EXDEV (otro volumen), EPERM/EACCES, ENOTSUP… y sus equivalentes de Windows
        log.debug("Enlace duro no disponible (%s); se copia el segmento", exc)
        shutil.copy2(src, dst)
        return "copy"


class BookmarkManager:
    def __init__(self, store: OpsStore, protected_dir: Path, recordings_dir: Callable[[], str],
                 segment_seconds: Callable[[], int], clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.protected_dir = Path(protected_dir)
        self.recordings_dir = recordings_dir
        self.segment_seconds = segment_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ alta
    def create(self, body: BookmarkCreate, user: str) -> Bookmark:
        start = _utc(body.start)
        end = _utc(body.end) if body.end else None
        if end is not None and end <= start:
            raise ValidationFailed("El final del tramo debe ser posterior al inicio",
                                   details={"fields": [{"loc": ["end"], "msg": "Debe ser posterior al inicio"}]})
        bm = Bookmark(id=new_bookmark_id(), camera_id=body.camera_id, start=start, end=end, note=body.note.strip(),
                      created_by=user, created_at=self.clock(), case_ref=body.case_ref.strip())
        if body.protect:
            bm = self._protect(bm, body.protect_reason, body.protect_days, user)
        self.store.save_bookmark(bm)
        self.store.add_timeline_event(TimelineEvent(camera_id=bm.camera_id, layer="bookmark", start=bm.start,
                                                    end=bm.end, severity="info", ref_id=bm.id,
                                                    title_es=bm.note or "Marcador"))
        return bm

    # ------------------------------------------------------------------ proteger
    def protect(self, bm: Bookmark, reason: str, days: int, user: str) -> Bookmark:
        bm = self._protect(bm, reason, days, user)
        self.store.save_bookmark(bm)
        return bm

    def _protect(self, bm: Bookmark, reason: str, days: int, user: str) -> Bookmark:
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise ValidationFailed("Para proteger un tramo hay que indicar el motivo",
                                   details={"fields": [{"loc": ["protect_reason"], "msg": "Campo obligatorio"}]})
        start = bm.start
        end = bm.end or (bm.start + timedelta(minutes=1))
        if end - start > MAX_PROTECT_SPAN:
            raise ValidationFailed("Un tramo protegido no puede pasar de 24 horas")
        segs = segments_in_range(Path(self.recordings_dir()), self.protected_dir, bm.camera_id, start, end,
                                 self.segment_seconds())
        if not segs:
            raise ValidationFailed("No hay grabación en ese tramo: no hay nada que proteger")
        folder = self.protected_dir / bm.camera_id / bm.id
        folder.mkdir(parents=True, exist_ok=True)
        names = []
        modes = set()
        for s in segs:
            dst = folder / s.name
            if not dst.exists():
                modes.add(link_or_copy(s.path, dst))
            names.append(s.name)
        until = self.clock() + timedelta(days=days)
        bm = bm.model_copy(update={"protected": True, "protect_reason": reason, "protect_until": until,
                                   "protected_by": user, "protected_files": names, "released_at": None,
                                   "release_reason": ""})
        atomic_write_text(folder / "meta.json", json.dumps({
            "bookmark_id": bm.id, "camera_id": bm.camera_id, "start": start.isoformat(), "end": end.isoformat(),
            "reason": reason, "case_ref": bm.case_ref, "until": until.isoformat(), "user": user,
            "files": names, "mode": sorted(modes) or ["existing"]}, ensure_ascii=False, indent=1))
        self.store.add_timeline_event(TimelineEvent(camera_id=bm.camera_id, layer="protected", start=start, end=end,
                                                    severity="info", ref_id=bm.id,
                                                    title_es=f"Tramo protegido: {reason}"))
        log.info("Tramo %s de %s protegido hasta %s por «%s» (%d segmentos)", bm.id, bm.camera_id,
                 until.date(), user, len(names))
        return bm

    def release(self, bm: Bookmark, reason: str) -> Bookmark:
        folder = self.protected_dir / bm.camera_id / bm.id
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        bm = bm.model_copy(update={"protected": False, "released_at": self.clock(), "release_reason": reason,
                                   "protected_files": []})
        self.store.save_bookmark(bm)
        return bm

    def delete(self, bm: Bookmark) -> None:
        if bm.protected:
            self.release(bm, "marcador borrado")
        self.store.delete_bookmark(bm.id)

    def expire(self) -> list[Bookmark]:
        """Libera los tramos cuya protección ha caducado. Devuelve los liberados."""
        now = self.clock()
        out = []
        for bm in self.store.bookmarks():
            if bm.protected and bm.protect_until is not None and _utc(bm.protect_until) <= now:
                out.append(self.release(bm, "caducada"))
                log.info("Caducó la protección del tramo %s de %s", bm.id, bm.camera_id)
        return out

    def protected_summary(self) -> dict[str, tuple[int, datetime | None]]:
        """Por cámara: número de tramos protegidos y fecha del más antiguo (para el informe)."""
        out: dict[str, tuple[int, datetime | None]] = {}
        for bm in self.store.bookmarks():
            if not bm.protected:
                continue
            n, oldest = out.get(bm.camera_id, (0, None))
            created = _utc(bm.created_at)
            out[bm.camera_id] = (n + 1, created if oldest is None or created < oldest else oldest)
        return out


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
