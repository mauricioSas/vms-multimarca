"""Doble de `python -m vms_updater` para la prueba de Windows de B1: no hace nada y para de forma ordenada
cuando `vmsctl run` cierra su entrada estándar (VMS_STOP_ON_STDIN_EOF=1). El de verdad lo entrega B4."""
from __future__ import annotations

import sys

if __name__ == "__main__":
    while sys.stdin is not None and sys.stdin.buffer.read(4096):
        pass
