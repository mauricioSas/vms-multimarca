"""Panel central: salud agregada, «tiendas con problemas hoy», auditoría y CSV de conteos (CONTRATO §18.16).

Dueño: B6. Fase 0 de la v2: router vacío ya registrado por `central/extensions.py`.
"""
from __future__ import annotations

from fastapi import APIRouter

from .extensions import CentralDeps


def build_router(deps: CentralDeps) -> APIRouter:
    return APIRouter(prefix="/api", tags=["ops"])
