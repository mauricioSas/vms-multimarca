"""Backend FastAPI (REST + proxy WHEP/playback + SSE) y servido de la interfaz web.

Contrato: docs/CONTRATO.md §6. Punto de entrada: `python -m vms` → vms.api.app:create_app(settings).
"""
from __future__ import annotations

from .app import build_state, create_app

__all__ = ["create_app", "build_state"]
