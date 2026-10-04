"""Usuarios locales (CONTRATO §6.2). Siempre debe quedar al menos un administrador habilitado."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from starlette.responses import Response

from vms.core.errors import ConflictError, NotFoundError
from vms.core.models import User, UserCreate, UserUpdate

from ..deps import Principal, get_state, require_admin
from ..errors import json_response
from ..security import hash_password_async
from ..state import AppState

log = logging.getLogger("vms.api.users")
router = APIRouter(prefix="/api/users", tags=["users"])


def _enabled_admins(users: list[User]) -> int:
    return sum(1 for u in users if u.role == "admin" and u.enabled)


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
async def update_user(username: str, body: UserUpdate, p: Principal = Depends(require_admin),
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
    updated = user.model_copy(update=changes)
    others = [u for u in state.users.all() if u.username.lower() != user.username.lower()]
    if _enabled_admins([*others, updated]) == 0:
        raise ConflictError("Debe quedar al menos un administrador habilitado")
    await state.users.save_user(updated)
    if not updated.enabled or "password_hash" in changes or updated.role != user.role:
        own = updated.username.lower() == p.username.lower() and updated.enabled
        # Tu propia sesión sigue; las demás sesiones de ese usuario se cierran
        state.sessions.delete_user(updated.username, keep=p.session.token if own else None)
    state.publish_config("users")
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
