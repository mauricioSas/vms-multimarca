"""Permisos por cámara (CONTRATO §18.8). Dueño: B6.

Las rutas de cámaras, vivo, grabaciones, descargas, muros (`PUT /api/walls/{monitor}`), analítica
(listas de reglas y de cámaras, estado) y `/api/status` llaman a estas funciones desde la fase 0 de la
v2; las rutas de B6 (salud, marcadores, evidencias, línea de tiempo) también.

Reglas (principio de acceso mínimo, art. 32 RGPD):
- administradores: todo (no tienen ámbito);
- kiosco: solo el vivo de las cámaras que están en alguno de los 4 muros (CONTRATO §18.8; los muros los monta
  un operador o un administrador). Una cámara que no está en ningún muro responde 404 al kiosco;
- operador sin `camera_scope`: todas las cámaras y acciones, como en la v1 (compatibilidad);
- operador con `camera_scope`: solo `scope.cameras` y solo las acciones marcadas (`live`, `playback`,
  `export`, `bookmark`). Una cámara fuera del ámbito responde 404: no se revela que existe.

El ámbito se lee del usuario en cada petición: un cambio en `PATCH /api/users/{username}` vale en el acto,
sin cerrar sesiones.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypeVar

from vms.core.models import CameraScope

from .deps import Principal
from .state import AppState

CameraAction = Literal["live", "playback", "export", "bookmark"]
T = TypeVar("T")


def scope_of(state: AppState, principal: Principal) -> CameraScope | None:
    """Ámbito del usuario (None = sin restricción)."""
    if principal.kiosk or principal.role == "admin":
        return None
    user = state.users.get(principal.username)
    return user.camera_scope if user is not None else None


def wall_camera_ids(state: AppState) -> set[str]:
    """Cámaras puestas en alguna celda de los 4 muros (lo único que ve el kiosco)."""
    return {c for w in state.config().walls for c in w.cells if c}


def camera_allowed(state: AppState, principal: Principal, camera_id: str, action: CameraAction) -> bool:
    """¿Puede `principal` hacer `action` sobre la cámara?"""
    if principal.role == "admin":
        return True
    if principal.kiosk:
        return action == "live" and camera_id in wall_camera_ids(state)
    scope = scope_of(state, principal)
    if scope is None:
        return True
    return camera_id in scope.cameras and bool(getattr(scope, action, False))


def ensure_camera_access(state: AppState, principal: Principal, camera_id: str, action: CameraAction) -> None:
    """Lanza NotFoundError si la cámara está fuera del ámbito del usuario."""
    if not camera_allowed(state, principal, camera_id, action):
        from vms.core.errors import NotFoundError
        raise NotFoundError("Cámara no encontrada")


def visible_camera_ids(state: AppState, principal: Principal, camera_ids: Iterable[str],
                       action: CameraAction) -> set[str]:
    """Subconjunto de `camera_ids` que el usuario puede ver para `action`."""
    return {cid for cid in camera_ids if camera_allowed(state, principal, cid, action)}


def filter_by_camera(state: AppState, principal: Principal, items: Iterable[T], action: CameraAction,
                     key: str = "camera_id") -> list[T]:
    """Solo los elementos (modelos o diccionarios con `key`) de cámaras visibles para `action`.

    Los elementos sin cámara (sin `key`) se conservan.
    """
    out: list[T] = []
    for item in items:
        cid: Any = item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)
        if not isinstance(cid, str) or camera_allowed(state, principal, cid, action):
            out.append(item)
    return out
