"""Páginas web servidas por el backend (CONTRATO §6.12).

Los archivos y su ruta pública los define vms.web (PAGES, WALL_PAGE, page_file). Aquí se añade
la comprobación de sesión en el servidor (además de la que hace cada página en el navegador):
  /            sin sesión de usuario → /login (o /setup si no hay usuarios y es el propio equipo)
  /login       público (si no hay usuarios y es el propio equipo → /setup)
  /setup       solo sin usuarios y desde el propio equipo; si no → /login
  /wall/1..4   cualquier sesión (la de kiosco primero: es un muro)
  resto        sesión de operador o administrador; la de kiosco no cuenta → /login

Las páginas del panel ignoran la cookie de kiosco (deps.py): en un navegador o perfil donde también hay muros
abiertos, el panel pide su propio inicio de sesión en vez de saltar al muro.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

from fastapi import APIRouter, Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from vms import web
from vms.core.models import MAX_MONITORS

from ..deps import get_state, principal_from
from ..security import is_local

log = logging.getLogger("vms.api.pages")
router = APIRouter(include_in_schema=False)

NO_STORE = {"Cache-Control": "no-store"}


def _file(name: str) -> Path | None:
    try:
        return web.page_file(name)
    except FileNotFoundError:
        return None


def _serve(name: str, title: str) -> Response:
    path = _file(name)
    if path is not None:
        return FileResponse(path, media_type="text/html; charset=utf-8", headers=NO_STORE)
    log.warning("Falta la página web «%s» en %s", name, web.PAGES_DIR)
    body = (f"<!doctype html><html lang='es'><meta charset='utf-8'><title>{html.escape(title)}</title>"
            "<body style='font-family:sans-serif;background:#111;color:#eee;padding:2rem'>"
            f"<h1>{html.escape(title)}</h1><p>La interfaz web no está instalada en este equipo. "
            "Reinstala el programa.</p></body></html>")
    return HTMLResponse(body, status_code=503, headers=NO_STORE)


def _login_redirect(request: Request) -> Response:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)


def _needs_setup(request: Request) -> bool:
    return not get_state(request).users.all() and is_local(request)


def _handler(route: str, name: str) -> Callable[[Request], Awaitable[Response]]:
    title = name.removesuffix(".html").capitalize()

    async def handler(request: Request) -> Response:
        if route == "/login":
            return RedirectResponse("/setup", status_code=303) if _needs_setup(request) else _serve(name, title)
        if route == "/setup":
            return _serve(name, title) if _needs_setup(request) else RedirectResponse("/login", status_code=303)
        p = principal_from(request, get_state(request), wall=False, allow_kiosk=False)
        if p is None:
            if route == "/" and _needs_setup(request):
                return RedirectResponse("/setup", status_code=303)
            return _login_redirect(request)
        return _serve(name, title)

    return handler


for _route, _name in web.PAGES.items():
    router.add_api_route(_route, _handler(_route, _name), methods=["GET"], name=f"page:{_name}")


@router.get("/wall/{monitor}")
async def wall_page(monitor: int, request: Request) -> Response:
    if not 1 <= monitor <= MAX_MONITORS:
        return HTMLResponse("<!doctype html><title>404</title><p>Monitor no válido (1 a 4)</p>", status_code=404)
    if principal_from(request, get_state(request), wall=True) is None:
        return _login_redirect(request)
    return _serve(web.WALL_PAGE, f"Monitor {monitor}")
