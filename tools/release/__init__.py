"""Herramientas de publicación de versiones (PLAN-V2 §1.6, §1.7 y §2.7). Dueño: B4.

Se ejecutan en el **PC de publicación** (nunca en una tienda ni en CI salvo `sign-meta`): firman `targets`
con la llave física (en desarrollo, claves software o SoftHSM marcadas `dev`), preparan los repositorios
TUF `online` y `offline`, los canales, la tabla de avisos, el espejo USB y los tokens de sede del Worker.

    python -m tools.release --help
"""
from __future__ import annotations

import sys
from pathlib import Path

# El formato de los paquetes y del descriptor es el del actualizador (`updater/vms_updater`): se reutiliza
# para que publicar y verificar no puedan divergir.
_UPDATER = Path(__file__).resolve().parents[2] / "updater"
if _UPDATER.is_dir() and str(_UPDATER) not in sys.path:
    sys.path.insert(0, str(_UPDATER))
