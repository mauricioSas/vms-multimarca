"""Punto de extensión del panel central para la v2 (fase 0). Dueño: arquitecto.

Las rutas de `central/app.py` se definen dentro de `create_app` y usan dependencias locales (sesión,
administrador, conexión a PostgreSQL). Para que B4 (versiones por sede) y B6 (salud agregada) añadan
rutas sin tocar `app.py`, cada uno expone `build_router(deps) -> APIRouter` y `create_app` los registra
pasándoles estas dependencias.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter


@dataclass(frozen=True)
class CentralDeps:
    session: Callable[..., Any]        # Depends(session) → Session (cualquier usuario con sesión)
    admin: Callable[..., Any]          # Depends(admin) → Session de administrador
    conn: Callable[..., Any]           # Depends(conn) → AsyncConnection de PostgreSQL
    now: Callable[[], datetime]
    settings: Any                      # CentralSettings


def extension_builders() -> list[Callable[[CentralDeps], APIRouter]]:
    from . import ops, updates
    return [updates.build_router, ops.build_router]
