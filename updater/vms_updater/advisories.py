"""Componente TUF `data`: tabla de avisos de seguridad entre versiones (PLAN-V2 §2.7, CONTRATO §18.12).

En cada comprobación se toma la entrada `data/advisories-*.json` con el `generated_at` más reciente de
`targets.json` firmado; si es más nueva que la instalada, se descarga (misma verificación TUF que el
resto), se valida el esquema 1 y se escribe con escritura atómica en `<datos>\\ops\\advisories\\advisories.json`.
Solo la escribe el actualizador. El backend (B6) elige entre esta y la de la versión la válida más reciente.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ._atomic import atomic_write_bytes, read_json
from .models import AdvisoryTableLite

log = logging.getLogger("vms_updater.advisories")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def latest_advisories_target(targets: dict[str, Any]) -> tuple[str, datetime] | None:
    best: tuple[str, datetime] | None = None
    for name, tf in targets.items():
        custom = getattr(tf, "custom", None) or {}
        if custom.get("kind") != "data" or custom.get("data") != "advisories" or custom.get("schema") != 1:
            continue
        gen = _parse_dt(custom.get("generated_at"))
        if gen is None:
            continue
        if best is None or gen > best[1]:
            best = (name, gen)
    return best


def installed_generated_at(path: Path) -> datetime | None:
    raw = read_json(path)
    if not isinstance(raw, dict):
        return None
    return _parse_dt(raw.get("generated_at"))


def install_advisories(data: bytes, dest: Path) -> datetime:
    """Valida y escribe la tabla. Lanza ValueError si no es válida (no se escribe nada)."""
    try:
        doc = json.loads(data)
        table = AdvisoryTableLite.model_validate(doc)
    except (ValueError, ValidationError) as exc:
        raise ValueError(f"tabla de avisos no válida: {exc}") from exc
    atomic_write_bytes(dest, data)
    log.info("Tabla de avisos instalada (generada %s, %d avisos)", table.generated_at.isoformat(),
             len(table.advisories))
    return table.generated_at
