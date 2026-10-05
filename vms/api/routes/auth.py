"""Autenticación (CONTRATO §6.2): login, logout, sesión actual, primer arranque y kiosco."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, SecretStr
from starlette.responses import RedirectResponse, Response

from vms.core.errors import AuthError, ConflictError, ForbiddenError, RateLimited
from vms.core.models import User, UserCreate

from ..deps import Principal, cookie_name, get_state, require_kiosk
from ..errors import json_response
from ..security import (Session, client_ip, constant_eq, hash_password_async, is_local,
                        verify_password_async)
from ..state import AppState

log = logging.getLogger("vms.api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=256)


def set_session_cookie(response: Response, request: Request, session: Session, max_age: int | None) -> None:
    """max_age None = cookie de sesión del navegador (kiosco: dura mientras el navegador esté abierto).

    El kiosco va en su propia cookie (`vms_kiosk`): entrar en los muros no cierra la sesión del panel del mismo
    navegador, ni iniciar sesión en el panel convierte los muros en operador (deps.py)."""
    response.set_cookie(cookie_name(session), session.token, max_age=max_age, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


def safe_next(value: str | None, default: str = "/wall/1") -> str:
    """Solo rutas internas relativas: evita redirecciones abiertas."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or "://" in value:
        return default
    return value


@router.post("/login")
async def login(body: LoginRequest, request: Request, state: AppState = Depends(get_state)) -> Response:
    ip = client_ip(request)
    wait = max(state.limiter.retry_after(ip, body.username), state.ip_limiter.retry_after(ip, "*"))
    if wait:
        raise RateLimited(f"Demasiados intentos fallidos. Espera {wait} segundos e inténtalo de nuevo.",
                          details={"retry_after": wait})
    user = state.users.get(body.username)
    password = body.password.get_secret_value()
    usable = user is not None and user.enabled
    ok = await verify_password_async(user.password_hash if usable and user else None, password)
    if not ok:
        state.limiter.failure(ip, body.username)
        state.ip_limiter.failure(ip, "*")
        log.warning("Inicio de sesión fallido para «%s» desde %s", body.username[:64], ip)
        raise AuthError("Usuario o contraseña incorrectos", code="invalid_credentials")
    assert user is not None
    state.limiter.success(ip, body.username)
    user = user.model_copy(update={"last_login_at": datetime.now(timezone.utc)})
    await state.users.save_user(user)
    session = state.sessions.create(user.username)
    log.info("Sesión iniciada: «%s» (%s) desde %s", user.username, user.role, ip)
    resp = json_response({"user": user.public()})
    set_session_cookie(resp, request, session, state.sessions.max_age)
    return resp


@router.post("/logout")
async def logout(request: Request, p: Principal = Depends(require_kiosk),
                 state: AppState = Depends(get_state)) -> Response:
    state.sessions.delete(p.session.token)
    resp = Response(status_code=204)
    # solo la cookie de esa sesión: cerrar sesión en el panel no saca a los muros del mismo navegador
    for name, value in request.cookies.items():
        if value == p.session.token:
            resp.delete_cookie(name, path="/")
    return resp


@router.get("/me")
async def me(p: Principal = Depends(require_kiosk)) -> Response:
    return json_response({"username": p.username, "role": p.role, "kiosk": p.kiosk})


@router.get("/setup")
async def setup_needed(state: AppState = Depends(get_state)) -> Response:
    return json_response({"needed": not state.users.all()})


@router.post("/setup")
async def setup(body: UserCreate, request: Request, state: AppState = Depends(get_state)) -> Response:
    if state.users.all():
        raise ConflictError("El sistema ya tiene usuarios: inicia sesión con un administrador")
    if not is_local(request):
        raise ForbiddenError("El primer administrador solo se puede crear desde el propio equipo (127.0.0.1)")
    user = User(username=body.username, role="admin",
                password_hash=await hash_password_async(body.password.get_secret_value()))
    await state.users.save_user(user)
    state.publish_config("users")
    log.warning("Primer administrador creado: «%s»", user.username)
    session = state.sessions.create(user.username)
    resp = json_response({"user": user.public()}, 201)
    set_session_cookie(resp, request, session, state.sessions.max_age)
    return resp


@router.get("/kiosk")
async def kiosk(request: Request, token: str = "", next: str = "/wall/1",
                state: AppState = Depends(get_state)) -> Response:
    expected = state.settings.kiosk_token.get_secret_value() if state.settings.kiosk_token else ""
    if not expected:
        raise ForbiddenError("El modo kiosco no está configurado (falta VMS_KIOSK_TOKEN)", code="kiosk_disabled")
    if not is_local(request) and not state.settings.kiosk_allow_remote:
        raise ForbiddenError("El modo kiosco solo funciona desde el propio equipo", code="kiosk_remote")
    ip = client_ip(request)
    wait = state.kiosk_limiter.retry_after(ip, "kiosk")
    if wait:
        raise RateLimited(f"Demasiados intentos. Espera {wait} segundos.", details={"retry_after": wait})
    if not token or not constant_eq(token, expected):
        state.kiosk_limiter.failure(ip, "kiosk")
        log.warning("Token de kiosco incorrecto desde %s", ip)
        raise AuthError("Token de kiosco no válido", code="invalid_kiosk_token")
    state.kiosk_limiter.success(ip, "kiosk")
    session = state.sessions.create("kiosco", kiosk=True)
    resp = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(resp, request, session, None)
    return resp
