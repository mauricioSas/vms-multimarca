"""Imágenes de referencia por cámara en `<datos>/ops/references/` (CONTRATO §18.2 y §18.17).

Es la ÚNICA imagen que guarda B6 de forma permanente (RGPD, docs/RGPD-EIPD.md §2.5):
- `<camera_id>-day.jpg` y `<camera_id>-night.jpg`: mediana de ~15 fotogramas (borra a quien pasa);
- `<camera_id>-alert.jpg`: la del último aviso de sabotaje, solo mientras el aviso sigue abierto
  (se borra al recuperarse la cámara);
- `<camera_id>.json`: metadatos (fecha, quién la fijó, fotogramas usados, movimiento medido).

Nunca viajan en avisos, en el latido ni en el informe. Se sirven bajo demanda a administradores y
cada lectura queda en `audit.log`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from vms.core.atomic import atomic_write_bytes, atomic_write_text
from vms.core.naming import is_valid_id
from vms.core.paths import restrict_permissions

from .imaging import Image, Reference, decode_image, encode_jpeg

log = logging.getLogger("vms.ops.references")

Kind = Literal["day", "night"]


class ReferenceStore:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self.folder)
        self._cache: dict[tuple[str, str, int], Reference] = {}

    def _path(self, camera_id: str, kind: str) -> Path:
        if not is_valid_id(camera_id):
            raise ValueError("Identificador de cámara no válido")
        return self.folder / f"{camera_id}-{kind}.jpg"

    def meta(self, camera_id: str) -> dict[str, Any]:
        f = self.folder / f"{camera_id}.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("Metadatos de referencia ilegibles para %s: %s", camera_id, exc)
            return {}

    def save(self, camera_id: str, kind: Kind, image: Image, *, user: str, frames: int, activity: float) -> datetime:
        at = datetime.now(timezone.utc)
        path = self._path(camera_id, kind)
        atomic_write_bytes(path, encode_jpeg(image, 92))
        restrict_permissions(path)
        meta = self.meta(camera_id)
        meta[kind] = {"at": at.isoformat(), "by": user, "frames": frames, "activity": activity}
        atomic_write_text(self.folder / f"{camera_id}.json", json.dumps(meta, ensure_ascii=False, indent=1))
        self._cache = {k: v for k, v in self._cache.items() if k[:2] != (camera_id, kind)}
        return at

    def jpeg(self, camera_id: str, kind: str) -> bytes | None:
        try:
            return self._path(camera_id, kind).read_bytes()
        except FileNotFoundError:
            return None

    def dates(self, camera_id: str) -> dict[Kind, datetime]:
        out: dict[Kind, datetime] = {}
        meta = self.meta(camera_id)
        for kind in ("day", "night"):
            item = meta.get(kind)
            if isinstance(item, dict) and self._path(camera_id, kind).is_file():
                try:
                    out[kind] = datetime.fromisoformat(str(item.get("at")))
                except ValueError:
                    continue
        return out

    def load(self, camera_id: str, kind: Kind, masks: list[list[tuple[float, float]]]) -> Reference | None:
        path = self._path(camera_id, kind)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        key = (camera_id, kind, hash((mtime_ns, json.dumps(masks))))
        ref = self._cache.get(key)
        if ref is None:
            img = decode_image(path.read_bytes())
            if img is None:
                log.warning("La referencia %s de %s está dañada; hay que volver a fijarla", kind, camera_id)
                return None
            ref = Reference.from_image(img, kind, masks)
            self._cache = {k: v for k, v in self._cache.items() if k[:2] != (camera_id, kind)}
            self._cache[key] = ref
        return ref

    # ---- imagen del último aviso de sabotaje (solo mientras el aviso está abierto)
    def save_alert(self, camera_id: str, image: Image) -> None:
        path = self._path(camera_id, "alert")
        atomic_write_bytes(path, encode_jpeg(image, 80))
        restrict_permissions(path)

    def clear_alert(self, camera_id: str) -> None:
        self._path(camera_id, "alert").unlink(missing_ok=True)

    def delete_camera(self, camera_id: str) -> None:
        for kind in ("day", "night", "alert"):
            self._path(camera_id, kind).unlink(missing_ok=True)
        (self.folder / f"{camera_id}.json").unlink(missing_ok=True)
        self._cache = {k: v for k, v in self._cache.items() if k[0] != camera_id}

    def camera_ids(self) -> set[str]:
        out = set()
        for f in self.folder.glob("*.json"):
            if is_valid_id(f.stem):
                out.add(f.stem)
        return out
