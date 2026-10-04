"""Motor de vídeo: genera mediamtx.yml, supervisa el proceso MediaMTX y habla con su API.

Contrato: docs/CONTRATO.md §4 y §5.2. Implementa vms.core.interfaces.Engine.
"""
from __future__ import annotations

from .engine import MediaMtxEngine, explain_source_error

__all__ = ["MediaMtxEngine", "explain_source_error"]
