"""Migraciones de config.json entre versiones (PLAN-V2 §2.8, CONTRATO §13.8). Dueño: B4.

Cadena de funciones PURAS sobre el JSON crudo: `MIGRATIONS[n]` convierte un documento de la versión
`n` en uno de la `n + 1`. Cada paso tiene su prueba con fixtures (`tests/core/fixtures/config_v1_*.json`).

Reglas de compatibilidad:
- Una versión que encuentra un `version` MAYOR que el suyo no migra ni reescribe nada
  (`newer_than_supported`): `ConfigStore` la carga en **solo lectura** y se niega a guardar
  (`NewerConfigError`). El rollback del actualizador restaura el respaldo, así que esto es una red de
  seguridad.
- Las funciones de migración nunca borran campos que no conocen (los modelos son `extra="allow"`).

El actualizador ejecuta la migración con el código de la versión NUEVA y sin arrancar el backend:

    python -m vms.core.config_migrations migrate --data-dir C:\\ProgramData\\VMSMultimarca

(idempotente: con config.json al día no escribe nada; escritura atómica).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text
from .errors import ConflictError
from .models import CONFIG_VERSION

Doc = dict[str, Any]


class NewerConfigError(ConflictError):
    """config.json es de una versión más nueva que este programa: no se guarda nunca (409 en la API)."""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(f"La configuración es de una versión más nueva ({found}; este programa entiende hasta "
                         f"la {supported}). Está en solo lectura para no perder datos: vuelve a la versión nueva "
                         "o restaura el respaldo.", code="config_read_only",
                         details={"found": found, "supported": supported})
        self.found = found
        self.supported = supported


def v1_to_v2(doc: Doc) -> Doc:
    """v1 → v2: solo campos nuevos con valor por defecto (allow_basic, follow_ip, identity, salud,
    avisos, ámbito por cámara). No hay que transformar nada: basta con subir la versión."""
    out = dict(doc)
    out["version"] = 2
    return out


MIGRATIONS: dict[int, Callable[[Doc], Doc]] = {1: v1_to_v2}


def document_version(doc: Doc) -> int:
    v = doc.get("version", 1)
    return v if isinstance(v, int) and v >= 1 else 1


def newer_than_supported(doc: Doc, supported: int | None = None) -> bool:
    return document_version(doc) > (CONFIG_VERSION if supported is None else supported)


def migrate(doc: Doc, *, target: int | None = None,
            migrations: dict[int, Callable[[Doc], Doc]] | None = None) -> tuple[Doc, list[int]]:
    """Aplica la cadena hasta `target` (por defecto CONFIG_VERSION). Devuelve (documento, versiones de
    origen aplicadas). Un documento más nuevo que `target` se devuelve tal cual."""
    goal = CONFIG_VERSION if target is None else target
    chain = MIGRATIONS if migrations is None else migrations
    applied: list[int] = []
    if not isinstance(doc, dict) or newer_than_supported(doc, goal):
        return doc, applied
    v = document_version(doc)
    while v < goal:
        step = chain.get(v)
        if step is None:
            raise ValueError(f"No hay migración de config.json desde la versión {v}")
        new = step(doc)
        if document_version(new) != v + 1:
            raise ValueError(f"La migración desde la versión {v} no dejó la versión {v + 1}")
        doc = new
        applied.append(v)
        v = document_version(doc)
    return doc, applied


def ensure_writable(doc: Doc, supported: int | None = None) -> None:
    """Lanza `NewerConfigError` si guardar `doc` pisaría un config.json de una versión más nueva."""
    sup = CONFIG_VERSION if supported is None else supported
    if newer_than_supported(doc, sup):
        raise NewerConfigError(document_version(doc), sup)


def migrate_file(path: Path) -> list[int]:
    """Migra config.json en su sitio (atómico). Sin archivo o ya al día: no hace nada."""
    path = Path(path)
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.json no es un objeto JSON")
    if newer_than_supported(raw):
        raise NewerConfigError(document_version(raw), CONFIG_VERSION)
    doc, applied = migrate(raw)
    if applied:
        atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2))
    return applied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vms.core.config_migrations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--data-dir", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        applied = migrate_file(a.data_dir / "config" / "config.json")
    except NewerConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"No se pudo migrar config.json: {exc}\n")
        return 1
    sys.stdout.write(json.dumps({"ok": True, "applied": applied, "version": CONFIG_VERSION}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
