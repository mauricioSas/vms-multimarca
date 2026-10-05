"""Permisos por cámara (CONTRATO §18.8, criterio 10 de B6) con la implementación REAL de `vms/api/permissions.py`.

Un operador con ámbito: 404 en cámaras fuera de su ámbito (vivo, grabación, descarga, reglas de analítica,
salud, línea de tiempo, marcadores), no las ve en `/api/status` ni en las listas, no puede añadirlas a un
muro (422) y el kiosco funciona igual que antes.
"""
from __future__ import annotations

from typing import Any

import httpx

from tests.ops.conftest import Harness, new_device

LIVE_SDP = b"v=0\r\noferta\r\n"


async def _setup(api: Harness) -> tuple[httpx.AsyncClient, httpx.AsyncClient, str, str, str]:
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    rule = await admin.post("/api/analytics/rules", json={"kind": "line", "camera_id": c2, "name": "Puerta",
                                                          "start": [0.1, 0.5], "end": [0.9, 0.5]})
    assert rule.status_code == 201
    op = await api.operator()
    r = await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [c1], "live": True,
                                                                        "playback": True, "export": False}})
    assert r.status_code == 200 and r.json()["camera_scope"]["cameras"] == [c1]
    await api.settle()
    return admin, op, c1, c2, rule.json()["id"]


async def test_out_of_scope_camera_is_404_everywhere(api: Harness) -> None:
    admin, op, c1, c2, rule_id = await _setup(api)
    t = {"start": "2026-10-04T10:00:00Z", "end": "2026-10-04T11:00:00Z"}
    cases: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", f"/api/cameras/{c2}", {}),
        ("GET", f"/api/live/{c2}", {}),
        ("GET", f"/api/recordings/{c2}/timeline", {}),
        ("GET", f"/api/recordings/{c2}/video", {"start": "2026-10-04T10:00:00Z", "duration": 10}),
        ("GET", f"/api/recordings/{c2}/video", {"start": "2026-10-04T10:00:00Z", "duration": 10, "download": "true"}),
        ("GET", f"/api/analytics/rules/{rule_id}", {}),
        ("GET", f"/api/camera-health/{c2}", {}),
        ("GET", f"/api/timeline/{c2}", t),
        ("GET", "/api/bookmarks", {"camera_id": c2}),
    ]
    for method, url, params in cases:
        r = await op.request(method, url, params=params)
        assert r.status_code == 404, (url, params, r.status_code, r.text)
    r = await op.post(f"/api/live/{c2}/sub/whep", content=LIVE_SDP, headers={"Content-Type": "application/sdp"})
    assert r.status_code == 404
    # la suya sí (vivo y reproducción); descarga no (export=False)
    assert (await op.get(f"/api/cameras/{c1}")).status_code == 200
    assert (await op.get(f"/api/live/{c1}")).status_code == 200
    assert (await op.get(f"/api/recordings/{c1}/timeline")).status_code == 200
    r = await op.get(f"/api/recordings/{c1}/video", params={"start": "2026-10-04T10:00:00Z", "duration": 10,
                                                            "download": "true"})
    assert r.status_code == 404, "sin permiso de exportar, la descarga no existe"


async def test_lists_and_status_are_filtered(api: Harness) -> None:
    admin, op, c1, c2, _ = await _setup(api)
    assert [c["id"] for c in (await op.get("/api/cameras")).json()] == [c1]
    assert {c["camera_id"] for c in (await op.get("/api/status")).json()["cameras"]} == {c1}
    assert [r["camera_id"] for r in (await op.get("/api/analytics/rules")).json()] == []
    assert [h["camera_id"] for h in (await op.get("/api/camera-health")).json()] == [c1]
    assert [s["camera_id"] for s in (await op.get("/api/recordings/summary")).json()] == [c1]
    report = (await op.get("/api/health-report")).json()
    assert [c["camera_id"] for c in report["cameras"]] == [c1]
    # el administrador lo sigue viendo todo
    assert {c["id"] for c in (await admin.get("/api/cameras")).json()} == {c1, c2}


async def test_cannot_add_out_of_scope_camera_to_a_wall_and_kiosk_unchanged(api: Harness) -> None:
    admin, op, c1, c2, _ = await _setup(api)
    r = await op.put("/api/walls/1", json={"cells": [c1, c2]})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"] == [{"loc": ["cells", 1],
                                                                                 "msg": "La cámara no existe"}]
    assert (await op.put("/api/walls/1", json={"cells": [c1]})).status_code == 200
    # el kiosco ve en vivo cualquier cámara de los muros, como en la v1
    assert (await admin.put("/api/walls/2", json={"cells": [c1, c2]})).status_code == 200
    k = await api.kiosk()
    assert (await k.get(f"/api/live/{c2}")).status_code == 200
    assert {c["id"] for c in (await k.get("/api/cameras")).json()} == {c1, c2}
    assert (await k.get(f"/api/recordings/{c1}/timeline")).status_code == 403   # el kiosco no reproduce


async def test_scope_validation_and_removal(api: Harness) -> None:
    admin, op, c1, c2, _ = await _setup(api)
    r = await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": ["cam-noexiste"]}})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["loc"] == ["camera_scope", "cameras", 0]
    r = await admin.patch("/api/users/admin", json={"camera_scope": {"cameras": [c1]}})
    assert r.status_code == 422, "los administradores no tienen ámbito"
    users = {u["username"]: u for u in (await admin.get("/api/users")).json()}
    assert users["operador"]["camera_scope"]["cameras"] == [c1] and users["admin"]["camera_scope"] is None
    # quitar el ámbito (null) = todas las cámaras otra vez, en el acto y sin cerrar la sesión
    assert (await admin.patch("/api/users/operador", json={"camera_scope": None})).status_code == 200
    assert {c["id"] for c in (await op.get("/api/cameras")).json()} == {c1, c2}
    # cambiar otra cosa no toca el ámbito
    await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [c2]}})
    await admin.patch("/api/users/operador", json={"enabled": True})
    assert [c["id"] for c in (await op.get("/api/cameras")).json()] == [c2]
    # al pasar a administrador se borra
    r = await admin.patch("/api/users/operador", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["camera_scope"] is None
