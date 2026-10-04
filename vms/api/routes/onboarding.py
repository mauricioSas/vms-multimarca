"""Asistente de primer uso, recorridos (Driver.js) vistos por usuario y ayuda contextual.

Router vacío creado en la fase 0 de la v2 y ya registrado en la aplicación (`vms/api/routes/__init__.py`):
el dueño añade aquí sus rutas sin tocar `app.py`. Contrato: CONTRATO §18.14. Dueño: B6.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])
