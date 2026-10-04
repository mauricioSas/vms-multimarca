"""Tipos MIME fijos para los archivos de la interfaz web.

En Windows, Python lee los tipos MIME del registro, y en muchos equipos «.js» figura como
«text/plain» (lo cambian algunos editores e instaladores). Con un tipo así el navegador se niega
a ejecutar los módulos JavaScript y la página se queda en blanco. Aquí se fijan los tipos
correctos sin depender del registro.
"""
from __future__ import annotations

import mimetypes

WEB_TYPES: dict[str, str] = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".map": "application/json",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def ensure_web_mimetypes() -> None:
    mimetypes.init()
    for ext, mime in WEB_TYPES.items():
        mimetypes.add_type(mime, ext)
