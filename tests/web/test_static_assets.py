"""Comprobaciones estáticas de la interfaz web (rápidas, sin navegador).

- Toda referencia /static/... de las páginas existe.
- Ninguna página ni script carga recursos de Internet (los muros funcionan sin salida a la red).
- Textos en español neutro: sin voseo.
- Sin menciones a herramientas internas en el entregable.
- vms.web.mount_web sirve las rutas del contrato (§6.12) y rechaza monitores fuera de 1..4.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from vms.web import MONITORS, PAGES, PAGES_DIR, STATIC_DIR, WALL_PAGE, mount_web

WEB_FILES = sorted(p for p in list(PAGES_DIR.glob("*.html")) + list(STATIC_DIR.rglob("*"))
                   if p.is_file() and p.suffix in {".html", ".js", ".css"})


def test_all_pages_exist() -> None:
    for name in [*PAGES.values(), WALL_PAGE]:
        assert (PAGES_DIR / name).is_file(), name


@pytest.mark.parametrize("page", sorted(PAGES_DIR.glob("*.html")), ids=lambda p: p.name)
def test_static_references_resolve(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, f"{page.name} no referencia ningún estático"
    for ref in refs:
        assert (STATIC_DIR / ref.removeprefix("/static/")).is_file(), f"{page.name}: falta {ref}"
    assert '<html lang="es">' in html
    assert '<meta name="viewport"' in html


def test_js_imports_resolve() -> None:
    js_dir = STATIC_DIR / "js"
    for f in js_dir.glob("*.js"):
        for mod in re.findall(r'from\s+"(\./[^"]+)"', f.read_text(encoding="utf-8")):
            assert (js_dir / mod).is_file(), f"{f.name} importa {mod}, que no existe"


def test_no_external_resources() -> None:
    pattern = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//|@import\s+url\(\s*["']?https?:|fetch\(\s*["']https?:""")
    for f in WEB_FILES:
        text = f.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{f.name} carga un recurso externo"
        assert "fonts.googleapis" not in text and "cdn." not in text, f"{f.name} usa un CDN"


VOSEO = re.compile(r"\b(vos|tenés|querés|podés|sabés|hacés|elegí|escribí|probá|mirá|fijate|tocá|andá|sos)\b",
                   re.IGNORECASE)


def test_spanish_without_voseo() -> None:
    for f in WEB_FILES:
        found = VOSEO.findall(f.read_text(encoding="utf-8"))
        assert not found, f"{f.name} contiene voseo: {found}"


def test_no_internal_tool_mentions() -> None:
    banned = re.compile(r"\b(claude|anthropic|chatgpt|openai|copilot|generad[oa] (?:por|con) ia)\b", re.IGNORECASE)
    for f in WEB_FILES:
        found = banned.findall(f.read_text(encoding="utf-8"))
        assert not found, f"{f.name} menciona {found}"


def test_no_inline_event_handlers_or_eval() -> None:
    """Sin onclick="..." ni eval: compatible con una CSP estricta (script-src 'self')."""
    for f in WEB_FILES:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=\s*\"", text) or f.suffix == ".js", f"{f.name} usa manejadores en línea"
        assert "eval(" not in text and "new Function(" not in text, f.name


async def test_mount_web_routes() -> None:
    app = FastAPI()
    mount_web(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://vms.local") as client:
        for route in PAGES:
            r = await client.get(route)
            assert r.status_code == 200, route
            assert r.headers["content-type"].startswith("text/html")
            assert r.headers["cache-control"] == "no-store"
        for m in MONITORS:
            r = await client.get(f"/wall/{m}")
            assert r.status_code == 200
            assert "wall.js" in r.text
        assert (await client.get("/wall/5")).status_code == 404
        assert (await client.get("/wall/0")).status_code == 404
        r = await client.get("/static/js/whep.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert (await client.get("/static/../__init__.py")).status_code == 404


ROOT = Path(__file__).resolve().parents[2]
PASSWORD_FORMS = [  # (página, script que engancha el envío)
    (ROOT / "vms/web/pages/login.html", ROOT / "vms/web/static/js/login.js"),
    (ROOT / "vms/web/pages/setup.html", ROOT / "vms/web/static/js/setup.js"),
    (ROOT / "central/web/login.html", ROOT / "central/web/static/login.js"),
    (ROOT / "central/web/setup.html", ROOT / "central/web/static/setup.js"),
]


@pytest.mark.parametrize(("page", "script"), PASSWORD_FORMS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_password_forms_never_submit_natively_with_get(page: Path, script: Path) -> None:
    """Regresión (prueba de sistema): un clic en «Entrar» antes de que cargara el script hacía que el
    navegador enviara el formulario por GET, con la contraseña en la URL. Ahora el botón llega
    desactivado, el script lo activa al enganchar el envío y el formulario es POST por si acaso."""
    html = page.read_text(encoding="utf-8")
    form = re.search(r"<form\b[^>]*>", html)
    assert form and 'method="post"' in form.group(0)
    button = re.search(r"<button\b[^>]*type=\"submit\"[^>]*>", html)
    assert button and re.search(r"\bdisabled\b", button.group(0))
    js = script.read_text(encoding="utf-8")
    assert "disabled = false" in js
    enable, attach = js.index("disabled = false"), js.index('addEventListener("submit"')
    assert enable < attach and "await" not in js[enable:attach]   # mismo turno: no hay hueco
