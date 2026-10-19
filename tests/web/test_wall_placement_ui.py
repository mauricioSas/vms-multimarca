"""Cámaras dadas de alta en «Monitores» y en el muro, en Chromium (CONTRATO §3.3, §6.3 y §6.11).

Caso real (08/10/2026): el usuario borró su cámara y la volvió a dar de alta. Se grababa y se veía en Reproducción,
pero no en «Monitores» ni en el muro. Ahora vuelve a su celda, el aviso del alta dice en qué monitor ha quedado, el
editor de monitores se pone al día sin «Cambios sin guardar» y el muro abierto la enseña sin recargar.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.conftest import get_free_port
from tests.web.conftest import ADMIN, login
from tests.web.demo import ServerThread
from tests.web.stub_backend import StubBackend

pytestmark = [pytest.mark.needs_browser, pytest.mark.slow]

CAMERA = {"name": "Entrada", "vendor": "hikvision", "kind": "camera", "host": "192.0.2.64", "username": "admin",
          "password": "secreto", "import_channels": [1]}


@pytest.fixture
def stub(fake_stub: tuple[StubBackend, ServerThread]) -> tuple[StubBackend, ServerThread]:
    backend, _ = fake_stub
    dead = get_free_port()   # el WHEP del FakeEngine a un puerto cerrado: «Sin señal», sin vídeo de verdad
    backend.engine.whep_url = lambda cid, stream: f"http://127.0.0.1:{dead}/{cid}/{stream}/whep"  # type: ignore[method-assign]
    return fake_stub


def api_client(server: ServerThread) -> httpx.Client:
    c = httpx.Client(base_url=server.base_url, headers={"X-Requested-With": "vms"}, timeout=10)
    c.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]}).raise_for_status()
    return c


def last_ok_toast(page: Any, text: str) -> str:
    page.wait_for_function("t => [...document.querySelectorAll('.toast.ok')].some(e => e.textContent.includes(t))",
                           arg=text, timeout=15000)
    return str(page.evaluate("t => [...document.querySelectorAll('.toast.ok')].map(e => e.textContent)"
                             ".find(x => x.includes(t))", text))


def test_re_added_camera_is_back_in_monitores_and_on_the_open_wall(stub: Any, make_context: Any) -> None:
    _, server = stub
    c = api_client(server)
    dev = c.post("/api/devices", json=CAMERA).json()
    c.put("/api/walls/1", json={"grid": 4, "cells": [None, None, dev["cameras"][0]]}).raise_for_status()
    c.delete(f"/api/devices/{dev['id']}").raise_for_status()

    ctx = make_context(server.base_url)
    wall = ctx.new_page()
    login(wall, next_path="/wall/1")
    wall.wait_for_selector(".cell >> nth=3")
    assert wall.locator(".cell:not(.empty)").count() == 0, "tras borrarla, el muro está vacío"
    wall.evaluate("() => { window.__noReload = true; }")

    page = ctx.new_page()
    page.goto("/")
    page.wait_for_selector("#monitor-screen .slot")
    page.click("#btn-add-device")
    page.fill("#dev-name", "Entrada")
    page.select_option("#dev-vendor", "hikvision")
    page.select_option("#dev-kind", "camera")
    page.fill("#dev-host", "192.0.2.64")
    page.fill("#dev-password", "secreto")
    page.click("#btn-device-save")
    text = last_ok_toast(page, "Equipo «Entrada» añadido")
    assert text == "Equipo «Entrada» añadido con 1 cámara. Añadida al monitor 1."

    cam = c.get("/api/cameras").json()[0]
    assert c.get("/api/walls/1").json()["cells"][:4] == [None, None, cam["id"], None], "vuelve a la celda 3"
    # el editor de «Monitores» la enseña en su celda y no dice «Cambios sin guardar»
    page.wait_for_function("() => document.querySelector(\"#monitor-screen [data-slot='2'] .slot-name\")"
                           "?.textContent === 'Cámara 1'", timeout=10000)
    assert page.input_value("[data-slot-select='2']") == cam["id"]
    assert page.is_hidden("#wall-dirty")
    # el muro abierto se pone al día solo (lo mismo hace el visor de escritorio, que abre esta página)
    wall.wait_for_function("() => document.querySelector(\".cell[data-index='2'] .label\")?.textContent === "
                           "'Cámara 1'", timeout=15000)
    assert wall.evaluate("() => window.__noReload") is True, "sin recargar la página"
    assert wall.evaluate("() => window.__vmsWall.layout.cells[2]") == cam["id"]


def test_toast_says_what_to_do_when_monitor_1_is_full(stub: Any, make_context: Any) -> None:
    _, server = stub
    c = api_client(server)
    dev = c.post("/api/devices", json={**CAMERA, "kind": "nvr", "name": "NVR", "import_channels": [1]}).json()
    c.put("/api/walls/1", json={"grid": 1}).raise_for_status()
    before = c.get("/api/walls").json()

    page = make_context(server.base_url).new_page()
    login(page)
    page.click("#btn-add-camera")
    page.select_option("#camera-form [name='device_id']", dev["id"])
    page.fill("#camera-form [name='name']", "Almacén")
    page.fill("#camera-form [name='channel']", "2")
    page.click("#btn-camera-save")
    text = last_ok_toast(page, "Cámara «Almacén» guardada")
    assert text == ("Cámara «Almacén» guardada. No había hueco libre en el monitor 1. Qué hacer: arrástrala a una "
                    "celda en «Monitores» y pulsa «Guardar monitor».")
    assert c.get("/api/walls").json() == before, "no pisa la celda ocupada ni toca otros monitores"


def test_import_with_unsaved_changes_keeps_them_and_adds_the_new_camera(stub: Any, make_context: Any) -> None:
    _, server = stub
    c = api_client(server)
    dev = c.post("/api/devices", json={**CAMERA, "kind": "nvr", "name": "NVR", "import_channels": [1]}).json()
    first = dev["cameras"][0]

    page = make_context(server.base_url).new_page()
    login(page)
    page.wait_for_selector("#monitor-screen [data-slot='0'] .slot-name")
    page.fill("#wall-name", "Mostrador")                   # cambio sin guardar
    assert page.is_visible("#wall-dirty")
    page.click(f"#devices-table tr[data-device='{dev['id']}'] [data-act='channels']")
    page.wait_for_selector("#channels-list .channel-item")
    page.click("#channels-none")
    page.check("#channels-list input[value='2']")
    page.check("#channels-list input[value='4']")
    page.click("#btn-channels-import")
    text = last_ok_toast(page, "dadas de alta")
    assert text == "2 cámaras dadas de alta. Añadidas al monitor 1."
    cams = {x["channel"]: x["id"] for x in c.get("/api/cameras").json()}
    assert c.get("/api/walls/1").json()["cells"][:4] == [first, cams[2], cams[4], None]
    # el borrador conserva el nombre sin guardar y suma las cámaras que el servidor ha puesto
    page.wait_for_function("() => document.querySelector(\"[data-slot-select='2']\").value === "
                           f"'{cams[4]}'", timeout=10000)
    assert page.is_visible("#wall-dirty") and page.input_value("#wall-name") == "Mostrador"
    page.click("#btn-save-wall")
    page.wait_for_selector("#wall-dirty", state="hidden")
    wall = c.get("/api/walls/1").json()
    assert wall["name"] == "Mostrador" and wall["cells"][:4] == [first, cams[2], cams[4], None], \
        "guardar no deshace lo que puso el alta"
