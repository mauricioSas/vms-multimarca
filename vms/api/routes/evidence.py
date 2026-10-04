"""Exportación de evidencias (SHA-256 + manifiesto firmado + visor portátil) y marcadores con bloqueo de retención.

Router vacío creado en la fase 0 de la v2 y ya registrado en la aplicación (`vms/api/routes/__init__.py`):
el dueño añade aquí sus rutas sin tocar `app.py`. Contrato: CONTRATO §18.6-§18.7 (marcadores y paquete). Dueño: B6.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["evidence"])
