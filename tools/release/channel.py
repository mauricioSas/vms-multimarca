"""Canales `pilot`, `stable`… (PLAN-V2 §2.7): mover, pausar o reanudar es una publicación firmada más.

    python -m tools.release channel stable --version 2.1.0
    python -m tools.release channel stable --pause
    python -m tools.release channel stable --resume

El paso de `pilot` a `stable` se hace por tandas cambiando el canal de las sedes desde el panel central (el
porcentaje por canal queda para la v2.1). «Revertir» una versión publicada = mover el canal a la anterior:
las tiendas no bajan solas (solo con rollback), pero las que no se hayan actualizado dejan de recibirla.
"""
from __future__ import annotations

from .publish import set_channel

__all__ = ["set_channel"]
