"""Estado de las actualizaciones de la sede para /status (lectura de public-status.json).

Router vacío creado en la fase 0 de la v2 y ya registrado en la aplicación (`vms/api/routes/__init__.py`):
el dueño añade aquí sus rutas sin tocar `app.py`. Contrato: CONTRATO §15.6. Dueño: B4.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/updates", tags=["updates"])
