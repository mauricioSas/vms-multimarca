"""Servicio actualizador `VMSUpdater` (PLAN-V2 §1.5 y §2.5, CONTRATO §15). Dueño: B4.

Corre como LocalSystem con su propia copia del runtime (ranuras A/B que elige `vmshost`). Solo instala lo
que viene en un `target` TUF verificado (python-tuf 7, `ngclient`) y deja cada paso anotado en un diario
(`state\\journal.json`) que sobrevive a un corte de luz.

Este paquete **no importa `vms`**: la ranura del actualizador lleva un runtime mínimo (tuf,
securesystemslib, cryptography, urllib3 y pydantic), no el código de la aplicación.
"""
from __future__ import annotations

__version__ = "2.0.0.dev0"
PRODUCT = "vms-multimarca"

__all__ = ["PRODUCT", "__version__"]
