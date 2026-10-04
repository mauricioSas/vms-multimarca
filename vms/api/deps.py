"""Dependencias de FastAPI: estado, usuario de la sesión y comprobación de rol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from vms.core.errors import AuthError, ForbiddenError

from .security import COOKIE_NAME, INTERNAL_HEADER, KIOSK_USERNAME, ROLE_RANK, Session, SessionRole, constant_eq
from .state import AppState


@dataclass(frozen=True)
class Principal:
    username: str
    role: SessionRole
    kiosk: bool
    session: Session

    def at_least(self, role: SessionRole) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def get_state(conn: HTTPConnection) -> AppState:
    return conn.app.state.vms  # type: ignore[no-any-return]


def principal_from(conn: HTTPConnection, state: AppState) -> Principal | None:
    session = state.sessions.get(conn.cookies.get(COOKIE_NAME))
    if session is None:
        return None
    if session.kiosk:
        state.sessions.touch(session)  # caducidad deslizante: el muro abierto no cae en /login
        return Principal(KIOSK_USERNAME, "kiosk", True, session)
    user = state.users.get(session.username)
    if user is None or not user.enabled:
        state.sessions.delete(session.token)
        return None
    return Principal(user.username, user.role, False, session)


def require(role: SessionRole) -> Callable[..., Principal]:
    def dependency(request: Request, state: AppState = Depends(get_state)) -> Principal:
        p = principal_from(request, state)
        if p is None:
            raise AuthError("Inicia sesión para continuar")
        if not p.at_least(role):
            raise ForbiddenError("No tienes permiso para esta acción" if not p.kiosk
                                 else "El modo kiosco solo permite ver los muros")
        return p
    return dependency


require_kiosk = require("kiosk")
require_operator = require("operator")
require_admin = require("admin")


def require_internal(request: Request, state: AppState = Depends(get_state)) -> None:
    token = request.headers.get(INTERNAL_HEADER, "")
    if not token or not constant_eq(token, state.internal_token):
        raise AuthError("Token interno no válido", code="invalid_internal_token")
