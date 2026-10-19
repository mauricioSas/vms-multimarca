"""Rutas de actualizaciones del backend (B4, CONTRATO §15.6): /api/updates/status, health deep y SSE `update`."""
from __future__ import annotations

import asyncio
import json

from vms import __version__
from vms.api.routes import updates as upd

from .conftest import Harness

HDR = {"X-VMS-Internal-Token": "token-interno-de-pruebas"}


async def test_status_requires_operator_and_reads_public_status(api: Harness) -> None:
    anon = api.client()
    assert (await anon.get("/api/updates/status")).status_code == 401
    kiosk = await api.kiosk()
    assert (await kiosk.get("/api/updates/status")).status_code == 403
    op = await api.operator()
    r = await op.get("/api/updates/status")
    assert r.status_code == 200
    assert r.json()["available_status"] is False and r.json()["state"] == "unknown"
    f = api.state.paths.base / "updater" / "public-status.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"schema": 1, "installed": "2.1.0", "channel": "pilot", "state": "good",
                             "last_result": "update_ok", "message_es": "Actualizado a la 2.1.0", "hold": False,
                             "token": "NO-DEBE-SALIR"}))
    body = (await op.get("/api/updates/status")).json()
    assert body["available_status"] and body["installed"] == "2.1.0" and body["channel"] == "pilot"
    assert "token" not in body and body["running"] == __version__


async def test_health_deep_requires_internal_token(api: Harness) -> None:
    anon = api.client()
    assert (await anon.get("/api/internal/health/deep")).status_code == 401
    admin = await api.login()
    assert (await admin.get("/api/internal/health/deep")).status_code == 401      # ni con sesión
    r = await anon.get("/api/internal/health/deep", headers=HDR)
    assert r.status_code == 200
    d = r.json()
    assert d["release"] == __version__ and d["engine"]["running"] is True
    assert d["cameras_total"] == 0 and d["cameras_recording"] == 0
    assert d["analytics"]["enabled"] is False and d["config_read_only"] is False


async def test_health_and_status_give_the_installed_release_version(api: Harness, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`vms.__version__` es 0.1.0 en todas las versiones publicadas: el visor enseñaba «Versión 0.1.0» del
    servicio y la web no notaba una actualización. Instalado, /api/health y /api/status dan la de release.json."""
    rel = tmp_path / "release.json"
    rel.write_text(json.dumps({"schema": 1, "product": "vms-multimarca", "version": "2.0.0-beta.5"}),
                   encoding="utf-8")
    monkeypatch.setattr(upd, "_release_file", lambda: rel)
    upd.running_version.cache_clear()
    try:
        h = (await api.client().get("/api/health")).json()
        assert h["version"] == "2.0.0-beta.5"
        assert set(h) == {"status", "version", "uptime_s", "engine"}      # el contrato no cambia de forma
        op = await api.operator()
        assert (await op.get("/api/status")).json()["version"] == "2.0.0-beta.5"
    finally:
        upd.running_version.cache_clear()
    # sin release.json (desarrollo y pruebas), la del paquete
    monkeypatch.setattr(upd, "_release_file", lambda: tmp_path / "no-existe.json")
    try:
        assert (await api.client().get("/api/health")).json()["version"] == __version__
    finally:
        upd.running_version.cache_clear()


async def test_watcher_emits_update_event_when_version_changes(api: Harness, tmp_path) -> None:  # type: ignore[no-untyped-def]
    q = api.state.bus.subscribe()
    f = api.state.paths.base / "updater" / "public-status.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"installed": "2.0.0", "state": "good", "last_result": "none"}))
    task = asyncio.create_task(upd._watch(api.state, interval=0.02))
    try:
        await asyncio.sleep(0.1)
        f.write_text(json.dumps({"installed": "2.1.0", "state": "good", "last_result": "update_ok",
                                 "message_es": "Actualizado"}))
        ev = None
        for _ in range(100):
            await asyncio.sleep(0.02)
            while not q.empty():
                name, data = q.get_nowait()
                if name == "update":
                    ev = json.loads(data) if isinstance(data, str) else data
            if ev:
                break
        assert ev is not None and ev["version"] == "2.1.0" and ev["viewer_restart"] is True
    finally:
        task.cancel()
        api.state.bus.unsubscribe(q)
