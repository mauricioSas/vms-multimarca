"""Permisos por cámara (CONTRATO §18.8). Dueño: B6.

Las rutas de cámaras, vivo, grabaciones y descargas ya llaman a estas funciones desde la fase 0 de
la v2, así B6 solo cambia este archivo para activar el ámbito por cámara (`User.camera_scope`).

Comportamiento actual (fase 0): todo permitido, exactamente como en la v1. Reglas que implementa B6:
- administradores: todo; kiosco: vivo de las cámaras de los muros;
- operador sin `camera_scope`: todas las cámaras (compatibilidad v1);
- operador con `camera_scope`: solo `scope.cameras` y solo las acciones marcadas (live, playback,
  export, bookmark). Una cámara fuera del ámbito responde 404 (no se revela que existe).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from .deps import Principal
from .state import AppState

CameraAction = Literal["live", "playback", "export", "bookmark"]


def camera_allowed(state: AppState, principal: Principal, camera_id: str, action: CameraAction) -> bool:
    """¿Puede `principal` hacer `action` sobre la cámara? (fase 0: siempre sí)."""
    return True


def ensure_camera_access(state: AppState, principal: Principal, camera_id: str, action: CameraAction) -> None:
    """Lanza NotFoundError si la cámara está fuera del ámbito del usuario (B6)."""
    if not camera_allowed(state, principal, camera_id, action):
        from vms.core.errors import NotFoundError
        raise NotFoundError("Cámara no encontrada")


def visible_camera_ids(state: AppState, principal: Principal, camera_ids: Iterable[str],
                       action: CameraAction) -> set[str]:
    """Subconjunto de `camera_ids` que el usuario puede ver para `action`."""
    return {cid for cid in camera_ids if camera_allowed(state, principal, cid, action)}
