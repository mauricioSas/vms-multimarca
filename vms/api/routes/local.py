"""Rutas para el visor de escritorio (intercambio del token de kiosco por archivo, diagnóstico).

Router vacío creado en la fase 0 de la v2 y ya registrado en la aplicación (`vms/api/routes/__init__.py`):
el dueño añade aquí sus rutas sin tocar `app.py`. Contrato: CONTRATO §17. Dueño: B2.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/local", tags=["local"])
