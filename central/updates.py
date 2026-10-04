"""Panel central: versiones por sede, canal, retener y rollback (CONTRATO §15.7). Dueño: B4.

Fase 0 de la v2: router vacío ya registrado por `central/extensions.py`.
"""
from __future__ import annotations

from fastapi import APIRouter

from .extensions import CentralDeps


def build_router(deps: CentralDeps) -> APIRouter:
    return APIRouter(prefix="/api", tags=["updates"])
