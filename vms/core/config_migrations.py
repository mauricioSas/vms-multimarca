"""Migraciones de config.json entre versiones (PLAN-V2 §2.8, CONTRATO §13.7). Dueño: B4.

Cadena de funciones PURAS sobre el JSON crudo: `MIGRATIONS[n]` convierte un documento de la versión
`n` en uno de la `n + 1`. Cada paso tiene su prueba con fixtures (`tests/core/fixtures/config_v1_*.json`).

Regla de compatibilidad: una versión que encuentra un `version` MAYOR que el suyo no migra ni
reescribe nada (`newer_than_supported`): lo decide ConfigStore (solo lectura, pendiente de B4).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import CONFIG_VERSION

Doc = dict[str, Any]


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


def newer_than_supported(doc: Doc) -> bool:
    return document_version(doc) > CONFIG_VERSION


def migrate(doc: Doc) -> tuple[Doc, list[int]]:
    """Aplica la cadena hasta CONFIG_VERSION. Devuelve (documento, versiones de origen aplicadas)."""
    applied: list[int] = []
    if not isinstance(doc, dict) or newer_than_supported(doc):
        return doc, applied
    v = document_version(doc)
    while v < CONFIG_VERSION:
        step = MIGRATIONS.get(v)
        if step is None:
            raise ValueError(f"No hay migración de config.json desde la versión {v}")
        doc = step(doc)
        applied.append(v)
        v = document_version(doc)
    return doc, applied
