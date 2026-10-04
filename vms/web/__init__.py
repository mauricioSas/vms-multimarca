"""Interfaz web estática (panel de control, muro por monitor, reproducción, dibujo de zonas).

HTML/CSS/JS sin paso de compilación (módulos ES nativos). Ninguna página carga recursos de
Internet: los muros deben funcionar en una red sin salida.

La autenticación se comprueba en el navegador: cada página llama a `GET /api/auth/me` y, si
recibe 401, redirige a `/login?next=<ruta>`. Por eso las páginas se pueden servir sin sesión.

El backend puede servirlas de dos formas:
  - `mount_web(app)` registra las rutas de §6.12 del contrato y monta `/static`.
  - o bien leer `PAGES` / `STATIC_DIR` y servirlas por su cuenta.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
PAGES_DIR = WEB_DIR / "pages"

# ruta pública → archivo HTML dentro de PAGES_DIR
PAGES: dict[str, str] = {
    "/": "index.html",
    "/login": "login.html",
    "/setup": "setup.html",
    "/playback": "playback.html",
    "/analytics": "analytics.html",
    "/status": "status.html",
}
WALL_PAGE = "wall.html"            # /wall/{1..4}
MONITORS = (1, 2, 3, 4)

# Las páginas no se cachean (cambian con cada versión); los estáticos se revalidan.
NO_STORE = {"Cache-Control": "no-store"}


def page_file(name: str) -> Path:
    path = (PAGES_DIR / name).resolve()
    if path.parent != PAGES_DIR or not path.is_file():
        raise FileNotFoundError(name)
    return path


def mount_web(app: Any) -> None:
    """Registra las páginas y `/static` en una aplicación FastAPI/Starlette.

    Se importa FastAPI aquí dentro para que `vms.web` no dependa de él al importarse.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    def _serve(name: str) -> Any:
        async def handler() -> FileResponse:
            return FileResponse(page_file(name), media_type="text/html; charset=utf-8", headers=NO_STORE)
        return handler

    for route, name in PAGES.items():
        app.add_api_route(route, _serve(name), methods=["GET"], include_in_schema=False,
                          name=f"web:{name}")

    async def wall(monitor: int) -> FileResponse:
        if monitor not in MONITORS:
            raise HTTPException(status_code=404, detail="Monitor no válido (1 a 4)")
        return FileResponse(page_file(WALL_PAGE), media_type="text/html; charset=utf-8", headers=NO_STORE)

    app.add_api_route("/wall/{monitor}", wall, methods=["GET"], include_in_schema=False, name="web:wall")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
