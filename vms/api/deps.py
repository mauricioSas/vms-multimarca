"""Dependencias de FastAPI: estado, usuario de la sesión y comprobación de rol.

Dos cookies, para que el panel y los muros convivan en el mismo navegador (o perfil de WebView2) sin pisarse:
`vms_session` (operador o administrador) y `vms_kiosk` (kiosco de los muros). Cada petición elige cuál vale:
las de un muro (cabecera `X-VMS-Client: wall`, o `?client=wall` en `/api/events`, que EventSource no deja
poner cabeceras) prefieren la de kiosco; las demás, la del usuario. Solo se elige entre sesiones que el
navegador ya tiene: no da más permisos que los que ya tiene.
"""
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


KIOSK_COOKIE = "vms_kiosk"
CLIENT_HEADER = "x-vms-client"
WALL_CLIENT = "wall"
SESSION_COOKIES = (COOKIE_NAME, KIOSK_COOKIE)


def cookie_name(session: Session) -> str:
    """Cookie que lleva esta sesión: la de kiosco o la del usuario."""
    return KIOSK_COOKIE if session.kiosk else COOKIE_NAME


def is_wall_request(conn: HTTPConnection) -> bool:
    """¿La petición sale de una página de muro (`/wall/N`)?"""
    if conn.headers.get(CLIENT_HEADER, "").strip().lower() == WALL_CLIENT:
        return True
    return conn.query_params.get("client") == WALL_CLIENT


def sessions_of(conn: HTTPConnection, state: AppState) -> tuple[Session | None, Session | None]:
    """(sesión de usuario, sesión de kiosco) válidas que trae la petición.

    Se clasifican por la sesión y no por el nombre de la cookie: una cookie de kiosco emitida antes de separar
    las cookies (en `vms_session`) sigue valiendo como kiosco."""
    user: Session | None = None
    kiosk: Session | None = None
    state.kiosk_token()   # si `vmsctl kiosk rotate` cambió el token, las cookies de kiosco anteriores ya no valen
    for name in SESSION_COOKIES:
        s = state.sessions.get(conn.cookies.get(name))
        if s is None:
            continue
        if s.kiosk:
            kiosk = kiosk or s
        else:
            user = user or s
    return user, kiosk


def _principal(session: Session, state: AppState) -> Principal | None:
    if session.kiosk:
        state.sessions.touch(session)  # caducidad deslizante: el muro abierto no cae en /login
        return Principal(KIOSK_USERNAME, "kiosk", True, session)
    user = state.users.get(session.username)
    if user is None or not user.enabled:
        state.sessions.delete(session.token)
        return None
    return Principal(user.username, user.role, False, session)


def principal_from(conn: HTTPConnection, state: AppState, *, wall: bool | None = None,
                   allow_kiosk: bool = True) -> Principal | None:
    """Quién hace la petición. `wall` = la hace un muro (por defecto se mira la cabecera); `allow_kiosk=False`
    ignora la sesión de kiosco (páginas del panel: un perfil con kiosco no entra en el panel)."""
    user, kiosk = sessions_of(conn, state)
    if not allow_kiosk:
        kiosk = None
    if wall is None:
        wall = is_wall_request(conn)
    for session in ((kiosk, user) if wall else (user, kiosk)):
        if session is not None:
            p = _principal(session, state)
            if p is not None:
                return p
    return None


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
