"""Usuarios locales (CONTRATO §6.2 y §18.8). Siempre debe quedar al menos un administrador habilitado.

v2 (B6): `PATCH /api/users/{username}` admite `camera_scope` (permisos por cámara de un operador):
`null` lo quita (todas las cámaras, como en la v1); un objeto lo fija. Los administradores no tienen ámbito.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import ConflictError, NotFoundError, ValidationFailed
from vms.core.models import CameraScope, User, UserCreate, UserUpdate

from ..deps import Principal, get_state, require_admin
from ..errors import json_response
from ..security import client_ip, hash_password_async
from ..state import AppState

log = logging.getLogger("vms.api.users")
router = APIRouter(prefix="/api/users", tags=["users"])


class UserScopeUpdate(UserUpdate):
    """PATCH con el ámbito por cámara (v2). `camera_scope` ausente = sin cambios; `null` = sin ámbito."""

    camera_scope: CameraScope | None = None


def _enabled_admins(users: list[User]) -> int:
    return sum(1 for u in users if u.role == "admin" and u.enabled)


def _check_scope(state: AppState, scope: CameraScope) -> CameraScope:
    cfg = state.config()
    unknown = [i for i, cid in enumerate(scope.cameras) if cfg.camera(cid) is None]
    if unknown:
        raise ValidationFailed("Hay cámaras que no existen en el ámbito", details={
            "fields": [{"loc": ["camera_scope", "cameras", i], "msg": "La cámara no existe"} for i in unknown]})
    return scope.model_copy(update={"cameras": list(dict.fromkeys(scope.cameras))})


@router.get("")
async def list_users(_: Principal = Depends(require_admin), state: AppState = Depends(get_state)) -> Response:
    return json_response([u.public() for u in state.users.all()])


@router.post("")
async def create_user(body: UserCreate, p: Principal = Depends(require_admin),
                      state: AppState = Depends(get_state)) -> Response:
    if state.users.get(body.username) is not None:
        raise ConflictError(f"Ya existe el usuario «{body.username}»")
    user = User(username=body.username, role=body.role,
                password_hash=await hash_password_async(body.password.get_secret_value()))
    await state.users.save_user(user)
    state.publish_config("users")
    log.info("Usuario «%s» (%s) creado por «%s»", user.username, user.role, p.username)
    return json_response(user.public(), 201)


@router.patch("/{username}")
async def update_user(username: str, body: UserScopeUpdate, request: Request, p: Principal = Depends(require_admin),
                      state: AppState = Depends(get_state)) -> Response:
    user = state.users.get(username)
    if user is None:
        raise NotFoundError(f"No existe el usuario «{username}»")
    changes: dict[str, object] = {}
    if body.role is not None:
        changes["role"] = body.role
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if body.password is not None:
        changes["password_hash"] = await hash_password_async(body.password.get_secret_value())
    role = body.role or user.role
    if "camera_scope" in body.model_fields_set:
        if body.camera_scope is not None and role == "admin":
            raise ValidationFailed("Los administradores ven todas las cámaras: el ámbito es solo para operadores",
                                   details={"fields": [{"loc": ["camera_scope"], "msg": "Solo para operadores"}]})
        changes["camera_scope"] = None if body.camera_scope is None else _check_scope(state, body.camera_scope)
    elif role == "admin" and user.camera_scope is not None:
        changes["camera_scope"] = None        # al pasar a administrador, el ámbito deja de tener sentido
    updated = User.model_validate({**user.model_dump(), **changes})
    others = [u for u in state.users.all() if u.username.lower() != user.username.lower()]
    if _enabled_admins([*others, updated]) == 0:
        raise ConflictError("Debe quedar al menos un administrador habilitado")
    await state.users.save_user(updated)
    if not updated.enabled or "password_hash" in changes or updated.role != user.role:
        own = updated.username.lower() == p.username.lower() and updated.enabled
        # Tu propia sesión sigue; las demás sesiones de ese usuario se cierran
        state.sessions.delete_user(updated.username, keep=p.session.token if own else None)
    state.publish_config("users")
    if "camera_scope" in changes:
        scope = updated.camera_scope
        audit("user_camera_scope", user=p.username, ip=client_ip(request), target=updated.username,
              cameras=scope.cameras if scope else "todas",
              actions=[a for a in ("live", "playback", "export", "bookmark") if scope and getattr(scope, a)])
    log.info("Usuario «%s» modificado por «%s» (%s)", updated.username, p.username, ", ".join(sorted(changes)) or "-")
    return json_response(updated.public())


@router.delete("/{username}")
async def delete_user(username: str, p: Principal = Depends(require_admin),
                      state: AppState = Depends(get_state)) -> Response:
    user = state.users.get(username)
    if user is None:
        raise NotFoundError(f"No existe el usuario «{username}»")
    if user.username.lower() == p.username.lower():
        raise ConflictError("No puedes borrar tu propio usuario")
    others = [u for u in state.users.all() if u.username.lower() != user.username.lower()]
    if _enabled_admins(others) == 0:
        raise ConflictError("No se puede borrar el último administrador habilitado")
    await state.users.delete_user(user.username)
    state.sessions.delete_user(user.username)
    state.publish_config("users")
    log.info("Usuario «%s» borrado por «%s»", user.username, p.username)
    return Response(status_code=204)
