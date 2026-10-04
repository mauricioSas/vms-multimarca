"""Asistente «¿por qué no conecta?» por reglas (LLM opcional solo para redactar).

Router vacío creado en la fase 0 de la v2 y ya registrado en la aplicación (`vms/api/routes/__init__.py`):
el dueño añade aquí sus rutas sin tocar `app.py`. Contrato: CONTRATO §18.11. Dueño: B6.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])
