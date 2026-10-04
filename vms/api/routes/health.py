"""Salud de imagen (0-100), informe de salud, desfase horario y previsión de días de grabación.

Router vacío creado en la fase 0 de la v2 y ya registrado en la aplicación (`vms/api/routes/__init__.py`):
el dueño añade aquí sus rutas sin tocar `app.py`. Contrato: CONTRATO §18.2-§18.5. Dueño: B6.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])   # /api/camera-health, /api/health-report, /api/clock, /api/retention-forecast
