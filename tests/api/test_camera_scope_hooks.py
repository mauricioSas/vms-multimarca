"""Ganchos de permisos por cámara (CONTRATO §18.8) en muros, analítica y /api/status.

En la fase 0 `camera_allowed` lo permite todo; aquí se sustituye por un ámbito de prueba («el operador
no ve la cámara 2») para comprobar que cada ruta pasa por `vms/api/permissions.py`, que es lo único que
B6 cambiará.
"""
from __future__ import annotations

import pytest

from vms.api import permissions
from vms.api.deps import Principal
from vms.api.state import AppState

from .conftest import Harness, new_device


@pytest.fixture
def hidden(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Cámaras fuera del ámbito de cualquier usuario que no sea administrador."""
    out: set[str] = set()

    def allowed(state: AppState, principal: Principal, camera_id: str, action: permissions.CameraAction) -> bool:
        return principal.role == "admin" or camera_id not in out

    monkeypatch.setattr(permissions, "camera_allowed", allowed)
    return out


async def test_operator_cannot_put_an_out_of_scope_camera_on_a_wall(api: Harness, hidden: set[str]) -> None:
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    hidden.add(c2)
    op = await api.operator()
    r = await op.put("/api/walls/1", json={"cells": [c1, c2]})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["fields"] == [{"loc": ["cells", 1], "msg": "La cámara no existe"}]
    assert (await op.get("/api/walls/1")).json()["cells"][:2] == [None, None]
    # el administrador sí puede; después el operador puede editar ese muro sin quitarla…
    assert (await admin.put("/api/walls/1", json={"cells": [None, c2]})).status_code == 200
    r = await op.put("/api/walls/1", json={"cells": [c1, c2], "name": "Cajas"})
    assert r.status_code == 200 and r.json()["cells"][:2] == [c1, c2]
    # …y quitarla, pero no volver a ponerla
    assert (await op.put("/api/walls/1", json={"cells": [c1]})).status_code == 200
    assert (await op.put("/api/walls/1", json={"cells": [c1, c2]})).status_code == 422


async def test_analytics_and_status_lists_respect_the_scope(api: Harness, hidden: set[str]) -> None:
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    rules = []
    for cam in (c1, c2):
        body = {"kind": "line", "camera_id": cam, "name": f"Entrada {cam}", "start": [0.1, 0.5], "end": [0.9, 0.5]}
        r = await admin.post("/api/analytics/rules", json=body)
        assert r.status_code == 201, r.text
        rules.append(r.json()["id"])
        assert (await admin.put(f"/api/analytics/cameras/{cam}", json={"enabled": True})).status_code == 200
    hidden.add(c2)
    op = await api.operator()

    assert [r["camera_id"] for r in (await op.get("/api/analytics/rules")).json()] == [c1]
    assert (await op.get("/api/analytics/rules", params={"camera_id": c2})).json() == []
    assert (await op.get(f"/api/analytics/rules/{rules[0]}")).status_code == 200
    r = await op.get(f"/api/analytics/rules/{rules[1]}")
    assert r.status_code == 404 and "regla" in r.json()["error"]["message"].lower()
    assert [a["camera_id"] for a in (await op.get("/api/analytics/cameras")).json()] == [c1]
    assert {c["camera_id"] for c in (await op.get("/api/status")).json()["cameras"]} == {c1}

    # el administrador lo sigue viendo todo
    assert len((await admin.get("/api/analytics/rules")).json()) == 2
    assert {c["camera_id"] for c in (await admin.get("/api/status")).json()["cameras"]} == {c1, c2}


def test_scoped_analytics_status_filters_cameras_only(hidden: set[str]) -> None:
    from vms.api.routes.analytics import scoped_analytics_status

    hidden.add("cam-2")
    op = Principal("op", "operator", False, None)  # type: ignore[arg-type]
    status = {"running": True, "cameras": [{"camera_id": "cam-1"}, {"camera_id": "cam-2"}, {"sin": "cámara"}]}
    out = scoped_analytics_status(None, op, status)  # type: ignore[arg-type]
    assert out["cameras"] == [{"camera_id": "cam-1"}, {"sin": "cámara"}] and out["running"] is True
    assert len(status["cameras"]) == 3, "no modifica el original"
    assert scoped_analytics_status(None, op, {"running": False}) == {"running": False}  # type: ignore[arg-type]
