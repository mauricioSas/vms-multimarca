"""Rutas para el visor de escritorio (CONTRATO §17.1). Dueño: B2.

Entrada de los muros sin escribir contraseñas y sin pasar el token por la línea de órdenes ni por la URL:

1. El visor (`VMS.exe`) lee `ProgramData\\VMSMultimarca\\secrets\\kiosk.token` (solo lo pueden leer SYSTEM,
   Administradores y el grupo `VMS Operadores`).
2. Abre en la ventana del muro `GET /api/local/kiosk`, una página mínima de este mismo origen, sin scripts.
3. Al terminar de cargar, ejecuta en esa página un `fetch` a `POST /api/local/kiosk-session` con
   `{"token": "…", "next": "/wall/N"}`: la respuesta (204) deja la cookie de kiosco firmada en el almacén del
   WebView y el script abre `/wall/N`.

Las dos rutas solo responden a peticiones del propio equipo (127.0.0.1 o ::1), aunque `VMS_KIOSK_ALLOW_REMOTE`
esté activado: es la entrada del visor local, no la del kiosco remoto de la v1 (`/api/auth/kiosk`).
"""
from __future__ import annotations

import html
import logging
import re

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, SecretStr, field_validator
from starlette.responses import HTMLResponse, Response

from vms.core.errors import AuthError, ForbiddenError, RateLimited

from ..deps import get_state
from ..security import client_ip, constant_eq, is_local
from ..state import AppState
from .auth import set_session_cookie

log = logging.getLogger("vms.api.local")
router = APIRouter(prefix="/api/local", tags=["local"])

WALL_PATH = re.compile(r"^/wall/[1-4]$")

# Página sin scripts propios: el visor ejecuta el intercambio desde fuera. connect-src 'self' permite ese fetch.
PAGE_CSP = ("default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")
PAGE_HEADERS = {"Cache-Control": "no-store", "Content-Security-Policy": PAGE_CSP, "Referrer-Policy": "no-referrer"}

ERRORS_ES = {
    "401": "El servicio no acepta la clave de los muros de este equipo.",
    "403": "La entrada de los muros solo funciona desde el propio equipo, con el modo kiosco activado.",
    "429": "Demasiados intentos seguidos. Se reintenta en unos segundos.",
    "red": "No hay conexión con el servicio.",
}


class KioskSessionRequest(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=512)
    next: str = Field(default="/wall/1", max_length=16)

    @field_validator("next")
    @classmethod
    def _wall_only(cls, v: str) -> str:
        if not WALL_PATH.match(v):
            raise ValueError("solo se admite /wall/1 a /wall/4")
        return v


def _page(title: str, message: str, status: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='robots' content='noindex'>"
        f"<title>{html.escape(title)}</title><style>html,body{{height:100%;margin:0}}"
        "body{display:flex;align-items:center;justify-content:center;background:#0a0c0f;color:#e8ebf0;"
        "font:20px/1.5 'Bahnschrift','Segoe UI',system-ui,sans-serif;text-align:center}"
        "small{display:block;color:#828c9b;font-size:15px;margin-top:8px}</style></head><body><div>"
        f"{html.escape(title)}<small>{html.escape(message)}</small></div></body></html>"
    )
    return HTMLResponse(body, status_code=status, headers=PAGE_HEADERS)


@router.get("/kiosk", include_in_schema=False)
async def kiosk_page(request: Request, error: str = "") -> Response:
    """Página donde el visor hace el intercambio. Con `?error=` solo muestra el motivo (no se repite nada)."""
    if not is_local(request):
        return _page("Solo en el propio equipo", "Esta página es para el visor de este PC.", 403)
    if error:
        return _page("No se pudo abrir el muro", ERRORS_ES.get(error[:8], "Se reintenta automáticamente."))
    return _page("Abriendo el muro…", "VMS Multimarca")


@router.post("/kiosk-session", status_code=204)
async def kiosk_session(body: KioskSessionRequest, request: Request,
                        state: AppState = Depends(get_state)) -> Response:
    """Cambia el token de kiosco (leído del archivo por el visor) por la cookie de kiosco firmada."""
    if not is_local(request):
        raise ForbiddenError("La entrada de los muros solo funciona desde el propio equipo", code="kiosk_remote")
    expected = state.settings.kiosk_token.get_secret_value() if state.settings.kiosk_token else ""
    if not expected:
        raise ForbiddenError("El modo kiosco no está configurado (falta el token de kiosco)", code="kiosk_disabled")
    ip = client_ip(request)
    wait = state.kiosk_limiter.retry_after(ip, "kiosk")
    if wait:
        raise RateLimited(f"Demasiados intentos. Espera {wait} segundos.", details={"retry_after": wait})
    if not constant_eq(body.token.get_secret_value(), expected):
        state.kiosk_limiter.failure(ip, "kiosk")
        log.warning("Token de kiosco incorrecto desde el visor (%s)", ip)
        raise AuthError("Token de kiosco no válido", code="invalid_kiosk_token")
    state.kiosk_limiter.success(ip, "kiosk")
    session = state.sessions.create("kiosco", kiosk=True)
    resp = Response(status_code=204, headers={"Cache-Control": "no-store"})
    set_session_cookie(resp, request, session, None)
    log.info("Sesión de kiosco abierta por el visor para %s", body.next)
    return resp
