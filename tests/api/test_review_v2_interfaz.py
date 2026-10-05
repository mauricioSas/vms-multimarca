"""Regresiones de la revisión cruzada de la v2 (docs/revision-v2/interfaz.md): token de kiosco, eventos por ámbito,
evento del motor y vigilancia de «seguir la IP». Cada prueba recrea la condición del fallo y afirma lo correcto.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from tests.api.conftest import Harness, new_device
from vms.api.deps import KIOSK_COOKIE, principal_from
from vms.api.events import EventBus
from vms.api.routes.events import scoped_event, status_event

OLD = "token-kiosco-de-pruebas"   # VMS_KIOSK_TOKEN de tests/conftest.py


def _write_token(api: Harness, token: str) -> None:
    """Como `vmsctl kiosk rotate` fuera de Windows (en Windows el archivo va con DPAPI de máquina)."""
    path = Path(api.state.paths.secrets_dir) / "kiosk.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # mtime distinto aunque sea en el mismo tick


# --------------------------------------------------------------------------- Seg M1: kiosk rotate
async def test_kiosk_rotate_is_honoured_by_the_backend(api: Harness) -> None:
    c = api.client()
    assert (await c.post("/api/local/kiosk-session", json={"token": OLD, "next": "/wall/1"})).status_code == 204
    old_cookie = c.cookies.get(KIOSK_COOKIE)
    assert (await c.get("/api/auth/me")).json()["kiosk"] is True

    _write_token(api, "token-nuevo-de-los-muros-0123456789")
    # el token del archivo (el que lee el visor) vale; el anterior ya no
    r = await api.client().post("/api/local/kiosk-session", json={"token": "token-nuevo-de-los-muros-0123456789",
                                                                  "next": "/wall/1"})
    assert r.status_code == 204, r.text
    r = await api.client().post("/api/local/kiosk-session", json={"token": OLD, "next": "/wall/1"})
    assert r.status_code == 401
    # la cookie de antes de rotar deja de valer (también como cookie firmada «recuperable»)
    stale = api.client()
    stale.cookies.set(KIOSK_COOKIE, old_cookie)
    assert (await stale.get("/api/auth/me")).status_code == 401


async def test_kiosk_token_file_works_without_env_variable(api: Harness) -> None:
    api.state.settings = api.state.settings.model_copy(update={"kiosk_token": None})
    r = await api.client().post("/api/local/kiosk-session", json={"token": OLD, "next": "/wall/1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "kiosk_disabled"
    _write_token(api, "token-del-instalador-abcdefghijklmn")
    r = await api.client().post("/api/local/kiosk-session", json={"token": "token-del-instalador-abcdefghijklmn",
                                                                  "next": "/wall/1"})
    assert r.status_code == 204, r.text


# --------------------------------------------------------------------------- A2: SSE por ámbito
async def _scoped_operator(api: Harness) -> tuple[Any, str, str]:
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    op = await api.operator()
    r = await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [c1], "live": True,
                                                                        "playback": True, "export": True}})
    assert r.status_code == 200
    return op, c1, c2


def _principal(api: Harness, client: Any, wall: bool = False) -> Any:
    class Conn:
        cookies = dict(client.cookies.items())
        headers: dict[str, str] = {}
        query_params = {"client": "wall"} if wall else {}

    p = principal_from(Conn(), api.state)   # type: ignore[arg-type]
    assert p is not None
    return p


async def test_sse_status_only_lists_cameras_in_scope(api: Harness) -> None:
    op, c1, c2 = await _scoped_operator(api)
    ev = (await status_event(api.state, _principal(api, op))).decode()
    assert c1 in ev and c2 not in ev
    admin_ev = (await status_event(api.state, _principal(api, await api.login()))).decode()
    assert c1 in admin_ev and c2 in admin_ev


async def test_sse_camera_events_respect_scope_and_owner(api: Harness) -> None:
    op, c1, c2 = await _scoped_operator(api)
    p = _principal(api, op)

    def sent(event: str, data: dict[str, Any]) -> dict[str, Any] | None:
        out = scoped_event(api.state, p, event, json.dumps(data))
        return None if out is None else json.loads(out.decode().split("data: ", 1)[1])

    assert sent("health", {"camera_id": c1, "score": 90}) is not None
    assert sent("health", {"camera_id": c2, "score": 90}) is None
    assert sent("bookmark", {"action": "created", "bookmark_id": "bm-1", "camera_id": c2}) is None
    assert sent("notice", {"title_es": "Cámara tapada: Almacén", "camera_ids": [c1, c2]}) is None
    assert sent("notice", {"title_es": "Disco casi lleno", "camera_ids": []}) is not None
    mine = sent("evidence", {"export_id": "ev-1", "state": "running", "_owner": "operador", "_cameras": [c1]})
    assert mine == {"export_id": "ev-1", "state": "running"}            # las claves internas no salen
    assert sent("evidence", {"export_id": "ev-2", "state": "running", "_owner": "otro", "_cameras": [c1]}) is None
    assert sent("engine", {"state": "down"}) == {"state": "down"}


async def test_sse_walls_get_no_notices_health_or_evidence(api: Harness) -> None:
    k = await api.kiosk()
    p = _principal(api, k, wall=True)
    assert p.kiosk
    for event in ("notice", "health", "bookmark", "evidence"):
        assert scoped_event(api.state, p, event, json.dumps({"camera_ids": [], "camera_id": "x"})) is None
    assert scoped_event(api.state, p, "engine", json.dumps({"state": "restarted"})) is not None


async def test_kiosk_only_sees_cameras_on_the_walls(api: Harness) -> None:
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    assert (await admin.put("/api/walls/3", json={"cells": [c1]})).status_code == 200
    k = await api.kiosk()
    ids = [c["id"] for c in (await k.get("/api/cameras")).json()]
    assert ids == [c1]
    assert (await k.get(f"/api/live/{c2}")).status_code == 404


# --------------------------------------------------------------------------- A3: evento del motor
async def test_engine_events_reach_the_event_bus(api: Harness) -> None:
    engine = api.state.engine
    assert callable(getattr(engine, "on_event", None)), "el arranque del backend no conectó el evento del motor"
    q = api.state.bus.subscribe()
    try:
        engine.on_event({"state": "down", "pid": None, "at": "2026-10-05T10:00:00Z"})
        event, data = await asyncio.wait_for(q.get(), 2.0)
    finally:
        api.state.bus.unsubscribe(q)
    assert event == "engine" and json.loads(data)["state"] == "down"


async def test_engine_events_from_another_thread_are_published_in_the_loop(api: Harness) -> None:
    q = api.state.bus.subscribe()
    try:
        await asyncio.to_thread(api.state.engine.on_event, {"state": "restarted", "pid": 7, "at": "x"})
        event, _ = await asyncio.wait_for(q.get(), 2.0)
    finally:
        api.state.bus.unsubscribe(q)
    assert event == "engine"


# --------------------------------------------------------------------------- «seguir la IP» en marcha
async def test_ip_watch_loop_runs_with_the_backend(api: Harness) -> None:
    names = {t.get_name() for t in asyncio.all_tasks()}
    assert "ip-watch" in names


def test_event_bus_payload_is_json() -> None:
    bus = EventBus()
    q = bus.subscribe()
    bus.publish("evidence", {"export_id": "ev-1", "_owner": "ana"})
    event, data = q.get_nowait()
    assert event == "evidence" and json.loads(data)["_owner"] == "ana"
    assert time.time() > 0
