"""Entrega en la tienda de lo que pide el panel central (CONTRATO §15.6).

La directiva llega en la respuesta del latido: por HTTP (`POST /api/heartbeat`, agente `VMSHeartbeat`) o
leída de PostgreSQL por el latido directo del backend (`VMSBackend`). Las dos vías la entregan igual: por la
tubería de control del actualizador (orden `directive`, que solo esas dos cuentas y los administradores
pueden usar). El actualizador (LocalSystem) la guarda en `updater\\central-directive.json`; los servicios no
tienen permiso de escritura en `updater\\`.

`None` = el panel ya no pide nada: el actualizador borra la directiva anterior y la tienda vuelve a lo que
configuró el instalador. Nunca lanza: un actualizador parado no puede tumbar el latido.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .updater_pipe_client import ServerNotTrusted, request

log = logging.getLogger("central.directive")


def deliver(directive: dict[str, Any] | None, updater_data: Path, *, timeout: float = 15.0) -> bool:
    """Entrega `directive` (`{"update": {...}}` de `central.updates.directive_for`, o None). True si se entregó."""
    update = directive.get("update") if isinstance(directive, dict) else None
    try:
        resp = request(updater_data, {"cmd": "directive", "directive": update}, timeout)
    except ServerNotTrusted as exc:
        log.error("No se entrega la directiva del panel: %s", exc)
        return False
    except (OSError, ValueError) as exc:
        log.info("No se pudo entregar la directiva del panel al actualizador (%s): se reintenta en el siguiente "
                 "latido", type(exc).__name__)
        return False
    if not resp.get("ok"):
        log.warning("El actualizador no aceptó la directiva del panel: %s", resp.get("message_es") or resp.get("error"))
        return False
    return True


__all__ = ["deliver"]
