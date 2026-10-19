"""Las cámaras dadas de alta aparecen en los monitores, con la app real (CONTRATO §3.3, §6.3, §6.4 y §6.11).

Caso real (08/10/2026): el usuario borró su cámara Hikvision y la volvió a dar de alta. Grababa y se veía en
Reproducción, pero no en «Monitores» ni en el muro: el borrado vació su celda y la cámara nueva no se ponía en ninguno.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from tests.api.conftest import Harness, new_device

CAMERA = {"name": "Entrada", "vendor": "hikvision", "kind": "camera", "host": "192.0.2.64", "username": "admin",
          "password": "Cl@ve#1", "import_channels": [1]}


async def _walls(c: httpx.AsyncClient) -> dict[int, dict[str, Any]]:
    return {w["monitor"]: w for w in (await c.get("/api/walls")).json()}


def _config_events(q: asyncio.Queue[tuple[str, str]]) -> list[str]:
    scopes = []
    while not q.empty():
        name, data = q.get_nowait()
        if name == "config":
            scopes.append(json.loads(data)["scope"])
    return scopes


async def test_deleting_and_re_adding_the_device_puts_the_camera_back_in_its_cell(api: Harness) -> None:
    admin = await api.login()
    first = await new_device(admin, **CAMERA)
    cam = first["cameras"][0]
    assert first["wall_placements"] == [{"camera_id": cam, "monitor": 1, "cell": 0, "restored": False}]
    # la tenía en la celda 3 del monitor 1 y también en el monitor 2
    assert (await admin.put("/api/walls/1", json={"grid": 4, "cells": [None, None, cam]})).status_code == 200
    assert (await admin.put("/api/walls/2", json={"grid": 1, "cells": [cam]})).status_code == 200

    assert (await admin.delete(f"/api/devices/{first['id']}")).status_code == 204
    walls = await _walls(admin)
    assert walls[1]["cells"][2] is None and walls[2]["cells"][0] is None
    saved = json.loads(api.state.paths.config_file.read_text(encoding="utf-8"))
    assert [(m["host"], m["vendor"], m["channel"], m["monitor"], m["cell"]) for m in saved["wall_memory"]] == [
        ("192.0.2.64", "hikvision", 1, 1, 2), ("192.0.2.64", "hikvision", 1, 2, 0)]
    assert "Cl@ve" not in json.dumps(saved["wall_memory"]) and "admin" not in json.dumps(saved["wall_memory"])

    q = api.state.bus.subscribe()
    try:
        again = await new_device(admin, **CAMERA)
        events = _config_events(q)
    finally:
        api.state.bus.unsubscribe(q)
    new = again["cameras"][0]
    assert new != cam
    assert again["wall_placements"] == [{"camera_id": new, "monitor": 1, "cell": 2, "restored": True},
                                        {"camera_id": new, "monitor": 2, "cell": 0, "restored": True}]
    walls = await _walls(admin)
    assert walls[1]["cells"][:4] == [None, None, new, None] and walls[2]["cells"][0] == new
    assert walls[2]["grid"] == 1, "no cambia la cuadrícula"
    # los muros abiertos (y el visor, que abre las mismas páginas) recargan su disposición con este aviso
    assert "walls" in events and events.index("walls") > events.index("devices")
    assert json.loads(api.state.paths.config_file.read_text(encoding="utf-8"))["wall_memory"] == []
    # el kiosco (muros del visor) la ve en vivo sin que nadie toque «Monitores»
    k = await api.kiosk()
    assert [c["id"] for c in (await k.get("/api/cameras")).json()] == [new]


async def test_new_cameras_fill_free_cells_of_monitor_1_only(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1, 2])
    c1, c2 = dev["cameras"]
    assert [p["cell"] for p in dev["wall_placements"]] == [0, 1]
    assert (await admin.put("/api/walls/1", json={"grid": 4, "cells": [c1, None, c2]})).status_code == 200
    assert (await admin.put("/api/walls/2", json={"grid": 9, "cells": [None, c2]})).status_code == 200
    others = {m: w for m, w in (await _walls(admin)).items() if m != 1}

    # alta manual: al primer hueco libre del monitor 1
    q = api.state.bus.subscribe()
    try:
        r = await admin.post("/api/cameras", json={"name": "Puerta", "device_id": dev["id"], "channel": 3})
        events = _config_events(q)
    finally:
        api.state.bus.unsubscribe(q)
    assert r.status_code == 201, r.text
    c3 = r.json()["id"]
    assert r.json()["wall_placements"] == [{"camera_id": c3, "monitor": 1, "cell": 1, "restored": False}]
    assert events == ["cameras", "walls"]

    # «Canales»: la primera nueva ocupa el último hueco; la segunda ya no cabe y no pisa nada
    r = await admin.post(f"/api/devices/{dev['id']}/channels/import", json={"channels": [4, 1, 3]})
    assert r.status_code == 201, r.text
    imported = r.json()
    assert [c["channel"] for c in imported] == [4]
    assert imported[0]["wall_placements"] == [{"camera_id": imported[0]["id"], "monitor": 1, "cell": 3,
                                               "restored": False}]
    walls = await _walls(admin)
    assert walls[1]["cells"][:5] == [c1, c3, c2, imported[0]["id"], None] and walls[1]["grid"] == 4
    api.behavior["channels"] = 8
    r = await admin.post(f"/api/devices/{dev['id']}/channels/import", json={"channels": [5]})
    assert r.status_code == 201 and r.json()[0]["wall_placements"] == []
    after = await _walls(admin)
    assert after[1] == walls[1], "con el monitor 1 lleno no se toca nada"
    assert {m: w for m, w in after.items() if m != 1} == others, "los monitores 2 a 4 no cambian"


async def test_device_created_without_cameras_places_nothing(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin)
    assert dev["cameras"] == [] and dev["wall_placements"] == []
    assert all(c is None for w in (await _walls(admin)).values() for c in w["cells"])
