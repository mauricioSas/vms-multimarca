"""Proveedores del latido de B6 (CONTRATO §7.3 bis): `health` y `evidence_key`.

Síncronos, rápidos y solo leen archivos locales (nunca red ni base de datos): el resumen del informe de
salud que deja el servicio de B6 en `<datos>/ops/health-summary.json` y la clave pública de evidencias de
`<datos>/ops/evidence-key.json`. Sin imágenes, IP ni secretos.
"""
from __future__ import annotations

from typing import Any

from vms.core.paths import AppPaths

from .evidence.keys import read_public
from .report import read_summary


def payload_health(paths: AppPaths) -> dict[str, Any] | None:
    return read_summary(paths.base / "ops")


def payload_evidence_key(paths: AppPaths) -> dict[str, Any] | None:
    pub = read_public(paths)
    return dict(pub) if pub else None
