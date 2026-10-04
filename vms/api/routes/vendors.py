"""Registro de drivers: GET /api/vendors (marcas, puertos, avisos y madurez).

Router vacío creado en la fase 0 de la v2 y ya registrado en la aplicación (`vms/api/routes/__init__.py`):
el dueño añade aquí sus rutas sin tocar `app.py`. Contrato: CONTRATO §16.4. Dueño: B5.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["vendors"])
