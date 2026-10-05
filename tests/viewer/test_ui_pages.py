"""Páginas locales del visor (`native/viewer/ui/`) en Chromium, con el IPC de Tauri simulado.

Comprueba que cada página pinta lo que devuelve el visor, que llama a los comandos correctos con los argumentos
correctos, que no hay errores de JavaScript y que se cumple la CSP del visor (sin estilos ni scripts en línea).
Con VMS_TEST_SCREENSHOTS=<carpeta> guarda una captura de cada página.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.web.conftest import pw_browser  # noqa: F401  (fixture: Chromium por módulo)

pytestmark = [pytest.mark.needs_browser]

UI = Path(__file__).resolve().parents[2] / "native" / "viewer" / "ui"
ORIGIN = "http://tauri.localhost"
FP_A = ":".join(["AB", "CD"] * 16)
FP_B = ":".join(["12", "EF"] * 16)

RESPONSES: dict[str, Any] = {
    "reintentar": {"estado": "conectando", "titulo": "Conectando con el servicio…",
                   "mensaje": "El servicio de vídeo no responde todavía. Se reintenta cada 2 segundos.",
                   "servidor": "local", "url": "http://127.0.0.1:8600"},
    "diagnostico": {"version_visor": "2.0.0", "carpeta_datos": "C:\\ProgramData\\VMSMultimarca",
                    "visor_json": "C:\\Users\\José\\AppData\\Roaming\\VMSMultimarca\\viewer.json",
                    "instalacion": "C:\\Program Files\\VMSMultimarca (versión 2.0.0)",
                    "servidor": {"nombre": "local", "url": "http://127.0.0.1:8600", "responde": True,
                                 "estado": "degraded", "version": "2.0.0", "detalle": "Motor de vídeo en marcha"},
                    "clave_muros": "sin_permiso",
                    "actualizaciones": {"installed": "2.0.0", "channel": "stable", "state": "good",
                                        "last_result": "update_ok", "last_check": "2026-11-20T03:10:00Z"},
                    "estado_actualizaciones": "Al día 2.0.0"},
    "servidores": {"servidores": [
        {"nombre": "local", "url": "http://127.0.0.1:8600", "remoto": False, "huella": None, "muros": [1, 2],
         "panel": True, "borrable": False},
        {"nombre": "Central", "url": "https://central:8643", "remoto": True, "huella": FP_A, "muros": [3],
         "panel": False, "borrable": True}],
        "muros": [[1, "local"], [2, "local"], [3, "Central"], [4, "local"]], "panel": "local"},
    "probar_servidor": {"url": "https://192.168.1.20:8643", "remoto": True, "huella": FP_B, "responde": True,
                        "mensaje": "Comprueba que esta huella es la que aparece en el servidor antes de guardarlo."},
    "certificado": {"servidor": "Central", "url": "https://central:8643", "esperada": FP_A, "observada": FP_B,
                    "primera_vez": False},
    "confiar_certificado": {"estado": "listo", "titulo": "Abriendo…", "mensaje": "", "servidor": "Central",
                            "url": "https://central:8643"},
    "sin_permiso": {"codigo": "sin_permiso", "titulo": "Sin permiso para abrir los muros",
                    "mensaje": "Tu usuario de Windows no está en el grupo «VMS Operadores».",
                    "archivo": "C:\\ProgramData\\VMSMultimarca\\secrets\\kiosk.token", "grupo": "VMS Operadores",
                    "muro": 2},
    "monitores": {"monitores": [
        {"clave": "\\\\.\\DISPLAY1|0,0|1920x1080", "nombre": "\\\\.\\DISPLAY1", "x": 0, "y": 0, "ancho": 1920,
         "alto": 1080, "escala": 1.0},
        {"clave": "\\\\.\\DISPLAY2|1920,0|3840x2160", "nombre": "\\\\.\\DISPLAY2", "x": 1920, "y": 0,
         "ancho": 3840, "alto": 2160, "escala": 1.5}],
        "muros": [{"muro": 1, "clave": "\\\\.\\DISPLAY1|0,0|1920x1080", "servidor": "local", "abierto": True,
                   "conectado": True},
                  {"muro": 2, "clave": "\\\\.\\DISPLAY3|0,1080|1920x1080", "servidor": "local", "abierto": True,
                   "conectado": False},
                  {"muro": 3, "clave": None, "servidor": "Central", "abierto": False, "conectado": False},
                  {"muro": 4, "clave": None, "servidor": "local", "abierto": False, "conectado": False}]},
    "acerca": {"producto": "VMS Multimarca", "version": "2.0.0", "avisos": "MediaMTX — MIT\nTauri — Apache-2.0 o MIT",
               "ruta_avisos": "C:\\Program Files\\VMSMultimarca\\versions\\2.0.0\\THIRD_PARTY_NOTICES.txt"},
    "actualizaciones": {"version_visor": "2.0.0", "etiqueta": "Disponible 2.1.0: se instalará en la ventana de "
                        "mantenimiento", "estado": {"installed": "2.0.0", "channel": "pilot",
                                                    "last_check": "2026-11-20T03:10:00Z"}, "puede_actuar": False},
}

IPC_MOCK = """
window.__ipcCalls = [];
window.__TAURI_INTERNALS__ = {
  invoke: async (cmd, args) => {
    window.__ipcCalls.push([cmd, args || {}]);
    const r = window.__ipcResponses[cmd];
    if (r === undefined) throw "comando desconocido: " + cmd;
    return JSON.parse(JSON.stringify(r));
  }
};
"""


@pytest.fixture
def ui_page(pw_browser: Any) -> Any:  # noqa: F811
    ctx = pw_browser.new_context(viewport={"width": 900, "height": 760}, locale="es-ES")
    ctx.add_init_script(f"window.__ipcResponses = {json.dumps(RESPONSES)};" + IPC_MOCK)

    def serve(route: Any) -> None:
        path = route.request.url.split(ORIGIN, 1)[1].split("?", 1)[0].lstrip("/") or "index.html"
        f = (UI / path).resolve()
        if UI not in f.parents or not f.is_file():
            route.fulfill(status=404, body="no existe")
            return
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        # la misma CSP que pone Tauri a las páginas locales (tauri.conf.json, sin los añadidos de IPC)
        route.fulfill(status=200, body=f.read_bytes(), headers={
            "Content-Type": ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""),
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:"})

    ctx.route(f"{ORIGIN}/**", serve)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.errors = errors  # type: ignore[attr-defined]
    yield page
    ctx.close()


def open_page(page: Any, name: str, query: str = "") -> None:
    page.goto(f"{ORIGIN}/{name}{query}")
    page.wait_for_load_state("networkidle")


def calls(page: Any, cmd: str | None = None) -> list[Any]:
    out = page.evaluate("() => window.__ipcCalls")
    return [c for c in out if cmd is None or c[0] == cmd]


def shot(page: Any, name: str) -> None:
    folder = os.environ.get("VMS_TEST_SCREENSHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"visor-{name}.png"), full_page=True)


def no_errors(page: Any) -> None:
    bad = [e for e in page.errors if "Content Security Policy" in e or e.startswith("pageerror")]
    assert bad == [], bad


def test_conectando_polls_every_two_seconds(ui_page: Any) -> None:
    open_page(ui_page, "conectando.html", "?ventana=muro-2")
    assert "Muro 2" in ui_page.inner_text("#titulo")
    assert "no responde" in ui_page.inner_text("#mensaje")
    ui_page.wait_for_timeout(4500)
    assert len(calls(ui_page, "reintentar")) >= 3
    assert "diagnostico.html?volver=" in ui_page.get_attribute("#diag", "href")
    shot(ui_page, "conectando")
    no_errors(ui_page)


def test_certificate_changed_requires_typing_the_new_fingerprint(ui_page: Any) -> None:
    open_page(ui_page, "certificado.html", "?ventana=muro-3")
    assert ui_page.is_visible("#cambiado") and not ui_page.is_visible("#primera")
    assert ui_page.inner_text("#c-esperada") == FP_A and ui_page.inner_text("#c-observada") == FP_B
    assert ui_page.is_disabled("#confiar")
    ui_page.fill("#ultimos", "0000")
    assert ui_page.is_disabled("#confiar")
    ui_page.fill("#ultimos", FP_B.replace(":", "")[-4:].lower())
    assert not ui_page.is_disabled("#confiar")
    shot(ui_page, "certificado-cambiado")
    ui_page.click("#confiar")
    ui_page.wait_for_function("() => window.__ipcCalls.some(c => c[0] === 'confiar_certificado')")
    assert calls(ui_page, "confiar_certificado")[0][1] == {"huella": FP_B}
    no_errors(ui_page)


def test_certificate_first_use(ui_page: Any) -> None:
    first = dict(RESPONSES["certificado"], esperada=None, primera_vez=True)
    ui_page.add_init_script(f"window.__ipcResponses.certificado = {json.dumps(first)};")
    open_page(ui_page, "certificado.html")
    assert ui_page.is_visible("#primera") and not ui_page.is_disabled("#confiar")
    shot(ui_page, "certificado-primera-vez")
    no_errors(ui_page)


def test_servers_probe_then_save_with_fingerprint(ui_page: Any) -> None:
    open_page(ui_page, "servidores.html")
    rows = ui_page.locator("#lista tr")
    assert rows.count() == 2
    assert "Quitar" in rows.nth(1).inner_text() and "Quitar" not in rows.nth(0).inner_text()
    assert ui_page.is_disabled("#guardar")
    ui_page.fill("#nombre", "Tienda 42")
    ui_page.fill("#url", "192.168.1.20:8643")
    ui_page.click("#probar")
    ui_page.wait_for_selector("#res-huella-caja:not([hidden])")
    assert ui_page.inner_text("#res-huella") == FP_B
    assert ui_page.input_value("#url") == "https://192.168.1.20:8643"
    shot(ui_page, "servidores")
    ui_page.click("#guardar")
    ui_page.wait_for_function("() => window.__ipcCalls.some(c => c[0] === 'guardar_servidor')")
    assert calls(ui_page, "guardar_servidor")[0][1] == {"nombre": "Tienda 42", "url": "https://192.168.1.20:8643",
                                                         "huella": FP_B}
    # cambiar la URL invalida la prueba anterior
    ui_page.fill("#url", "https://otra:8643")
    assert ui_page.is_disabled("#guardar")
    ui_page.select_option("#asig-muro-4", "Central")
    ui_page.wait_for_function("() => window.__ipcCalls.some(c => c[0] === 'asignar_servidor_muro')")
    assert calls(ui_page, "asignar_servidor_muro")[0][1] == {"muro": 4, "servidor": "Central"}
    no_errors(ui_page)


def test_sin_permiso_explains_the_group(ui_page: Any) -> None:
    open_page(ui_page, "sin-permiso.html", "?ventana=muro-2")
    assert ui_page.inner_text("#titulo") == "Muro 2: Sin permiso para abrir los muros"
    assert "VMS Operadores" in ui_page.inner_text("main")
    assert "kiosk.token" in ui_page.inner_text("#archivo")
    shot(ui_page, "sin-permiso")
    no_errors(ui_page)


def test_monitors_assignment(ui_page: Any) -> None:
    open_page(ui_page, "monitores.html")
    assert ui_page.locator("#monitores tr").count() == 2
    assert "150 %" in ui_page.inner_text("#monitores")
    assert "Oculto" in ui_page.inner_text("#muros")
    assert ui_page.locator("#monitor-2 option", has_text="desconectado").count() == 1
    shot(ui_page, "monitores")
    ui_page.select_option("#monitor-3", "\\\\.\\DISPLAY2|1920,0|3840x2160")
    ui_page.wait_for_function("() => window.__ipcCalls.some(c => c[0] === 'asignar_monitor')")
    assert calls(ui_page, "asignar_monitor")[0][1] == {"muro": 3, "clave": "\\\\.\\DISPLAY2|1920,0|3840x2160"}
    no_errors(ui_page)


def test_diagnostics_and_about_and_updates(ui_page: Any) -> None:
    open_page(ui_page, "diagnostico.html", "?volver=conectando.html%3Fventana%3Dpanel")
    assert "con avisos" in ui_page.inner_text("#srv-estado")
    assert "VMS Operadores" in ui_page.inner_text("#clave")
    assert ui_page.is_visible("#volver")
    shot(ui_page, "diagnostico")
    no_errors(ui_page)
    # un «volver» que no es una página local no se usa
    open_page(ui_page, "diagnostico.html", "?volver=https%3A%2F%2Fevil.example%2F")
    assert not ui_page.is_visible("#volver")
    open_page(ui_page, "acerca.html")
    assert "Apache-2.0" in ui_page.inner_text("#avisos")
    shot(ui_page, "acerca")
    open_page(ui_page, "actualizaciones.html")
    assert "Disponible 2.1.0" in ui_page.inner_text("#etiqueta")
    assert ui_page.is_disabled("#buscar") and ui_page.is_disabled("#volver")
    shot(ui_page, "actualizaciones")
    no_errors(ui_page)


def test_pages_have_no_inline_code_and_only_local_resources() -> None:
    for f in sorted(UI.glob("*.html")):
        html = f.read_text(encoding="utf-8")
        assert '<html lang="es">' in html, f.name
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), f"{f.name}: script en línea"
        assert " style=" not in html, f"{f.name}: estilo en línea (la CSP lo bloquea)"
        assert not re.search(r"\son[a-z]+=", html), f"{f.name}: manejador en línea"
        for ref in re.findall(r'(?:src|href)="([^"#?]+)', html):
            assert "//" not in ref, f"{f.name}: recurso externo {ref}"
            assert (UI / ref).is_file(), f"{f.name}: falta {ref}"
    for f in sorted(UI.glob("*.js")):
        js = f.read_text(encoding="utf-8")
        assert "innerHTML" not in js and "eval(" not in js and "new Function" not in js, f.name
