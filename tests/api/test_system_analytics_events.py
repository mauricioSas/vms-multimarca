"""Salud, estado, ajustes, analítica, configuración interna y eventos SSE (CONTRATO §6.8-6.11, §8.2)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx

from tests.api.conftest import ADMIN_PW, HEADERS, Harness, new_device


async def test_health_is_public_and_reflects_engine(api: Harness) -> None:
    c = api.client()
    h = (await c.get("/api/health")).json()
    assert h["status"] == "ok" and h["version"] == "0.1.0" and h["engine"] == {"running": True, "api_ok": True}
    assert set(h) == {"status", "version", "uptime_s", "engine"}
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1, 2])
    await api.settle()
    api.engine.offline.add(dev["cameras"][1])
    api.state._paths_cache = None
    assert (await c.get("/api/health")).json()["status"] == "degraded"
    st = (await admin.get("/api/status")).json()
    assert st["status"] == "degraded" and "cámaras sin vídeo" in st["problems"]
    by_id = {x["camera_id"]: x for x in st["cameras"]}
    assert by_id[dev["cameras"][0]]["online"] is True and by_id[dev["cameras"][1]]["online"] is False
    assert st["engine"]["running"] is True and st["disk"]["percent"] == 40.0
    assert st["credential_backend"] == "file" and st["analytics"]["running"] is False
    await api.engine.stop()
    assert (await c.get("/api/health")).json()["status"] == "down"


async def test_settings_patch_and_retention(api: Harness) -> None:
    admin = await api.login()
    s = (await admin.get("/api/settings")).json()
    assert s["site"]["id"] == "site-test"  # tomado de VMS_SITE_ID en el primer arranque
    r = await admin.patch("/api/settings", json={"site": {"name": "Tienda Gràcia"},
                                                  "alerts": {"telegram_enabled": True, "telegram_chat_id": "-100123"}})
    assert r.status_code == 200
    s = r.json()
    assert s["site"]["name"] == "Tienda Gràcia" and s["site"]["timezone"] == "Europe/Madrid"
    assert s["alerts"] == {"telegram_enabled": True, "telegram_chat_id": "-100123"}
    assert (await admin.patch("/api/settings", json={"inventado": 1})).status_code == 422
    r = await admin.patch("/api/settings", json={"retention": {"days": 0}})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["loc"][:2] == ["retention", "days"]

    r = await admin.put("/api/settings/retention", json={"days": 15, "disk_guard_percent": 85})
    assert r.status_code == 200 and r.json() == {"days": 15, "disk_guard_percent": 85}
    await api.settle()
    assert api.engine.retention is not None and api.engine.retention.days == 15
    op = await api.operator()
    assert (await op.get("/api/settings/retention")).json()["days"] == 15
    assert (await op.put("/api/settings/retention", json={"days": 1})).status_code == 403


async def test_analytics_rules_cameras_and_internal_config(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1, 2])
    c1, c2 = dev["cameras"]
    line = {"kind": "line", "camera_id": c1, "name": "Entrada", "start": [0.1, 0.55], "end": [0.9, 0.55]}
    r = await admin.post("/api/analytics/rules", json={**line, "id": "rule-inventado"})
    assert r.status_code == 201
    rule = r.json()
    assert rule["id"].startswith("rule-") and rule["id"] != "rule-inventado" and rule["invert"] is False
    zone = {"kind": "zone", "camera_id": c2, "name": "Cola cajas", "polygon": [[0, 0], [0.5, 0], [0.5, 0.5]],
            "alert_threshold": 4, "alert_min_seconds": 30}
    zr = (await admin.post("/api/analytics/rules", json=zone)).json()
    bad = await admin.post("/api/analytics/rules", json={**line, "start": [1.5, 0]})
    assert bad.status_code == 422
    assert (await admin.post("/api/analytics/rules", json={**line, "kind": "circle"})).status_code == 422
    assert (await admin.post("/api/analytics/rules", json={**line, "camera_id": "cam-00000000"})).status_code == 422
    assert [r["id"] for r in (await admin.get("/api/analytics/rules", params={"camera_id": c2})).json()] == [zr["id"]]

    r = await admin.put(f"/api/analytics/rules/{rule['id']}", json={**line, "name": "Puerta norte", "invert": True})
    assert r.status_code == 200 and r.json()["invert"] is True and r.json()["id"] == rule["id"]
    r = await admin.put(f"/api/analytics/rules/{rule['id']}", json={**zone, "camera_id": c1})
    assert r.status_code == 422  # no se puede cambiar el tipo
    r = await admin.put(f"/api/analytics/rules/{rule['id']}", json={**line, "camera_id": c2})
    assert r.status_code == 422  # ni la cámara

    assert (await admin.get("/api/analytics/cameras")).json() == []  # nada guardado todavía
    r = await admin.put(f"/api/analytics/cameras/{c1}", json={"enabled": True, "fps": 12, "detector": "rfdetr-small"})
    assert r.status_code == 200 and r.json()["camera_id"] == c1
    assert [a["camera_id"] for a in (await admin.get("/api/analytics/cameras")).json()] == [c1]
    assert (await admin.put(f"/api/analytics/cameras/{c1}", json={"enabled": True, "detector": "rfdetr-xl"})
            ).status_code == 422  # los tamaños XL/2XL no están permitidos
    assert (await admin.put("/api/analytics/cameras/cam-00000000", json={"enabled": True})).status_code == 404

    anon = api.client()
    assert (await anon.get("/api/internal/analytics/config")).status_code == 401
    hdr = {"X-VMS-Internal-Token": "token-interno-de-pruebas"}
    r = await anon.get("/api/internal/analytics/config", headers=hdr)
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["site"]["id"] == "site-test" and cfg["alerts"]["telegram_enabled"] is False
    assert [c["camera_id"] for c in cfg["cameras"]] == [c1]  # c2 no tiene la analítica activada
    cam = cfg["cameras"][0]
    assert cam["rtsp_url"] == f"rtsp://127.0.0.1:8554/{c1}/sub" and cam["fps"] == 12.0
    assert cam["detector"] == "rfdetr-small" and cam["rules"][0]["name"] == "Puerta norte"
    assert "Cl@ve" not in r.text and "admin:" not in r.text
    etag = r.headers["etag"]
    assert (await anon.get("/api/internal/analytics/config", headers={**hdr, "If-None-Match": etag})).status_code == 304
    await admin.patch(f"/api/devices/{dev['id']}", json={"name": "Otro nombre"})  # no cambia la analítica
    assert (await anon.get("/api/internal/analytics/config", headers={**hdr, "If-None-Match": etag})).status_code == 304
    await admin.delete(f"/api/analytics/rules/{rule['id']}")
    r = await anon.get("/api/internal/analytics/config", headers={**hdr, "If-None-Match": etag})
    assert r.status_code == 200 and r.json()["cameras"] == []  # sin reglas activas no se analiza


async def test_analytics_status_file(api: Harness) -> None:
    admin = await api.login()
    assert (await admin.get("/api/analytics/status")).json() == {"running": False, "stale": True}
    f = api.state.paths.analytics_dir / "status.json"
    now = datetime.now(timezone.utc)
    f.write_text(json.dumps({"running": True, "updated_at": now.isoformat().replace("+00:00", "Z"), "cameras": []}))
    st = (await admin.get("/api/analytics/status")).json()
    assert st["running"] is True and st["stale"] is False
    f.write_text(json.dumps({"running": True, "updated_at": (now - timedelta(minutes=2)).isoformat()}))
    assert (await admin.get("/api/analytics/status")).json()["stale"] is True
    f.write_text("{roto")
    assert (await admin.get("/api/analytics/status")).json()["running"] is False
    f.write_text("[1, 2]")
    assert (await admin.get("/api/analytics/status")).json() == {"running": False, "stale": True,
                                                                  "error": "estado ilegible"}


async def test_sse_streams_status_and_config_events(api: Harness, live_server: str) -> None:
    async with httpx.AsyncClient(base_url=live_server, headers=HEADERS, timeout=10) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200

        async def read_events(lines: AsyncIterator[str], want: set[str]) -> dict[str, dict]:
            got: dict[str, dict] = {}
            event = ""
            async for line in lines:
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    got[event] = json.loads(line[6:])
                    if want <= set(got):
                        return got
            return got

        async with c.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
            lines = resp.aiter_lines()
            first = await asyncio.wait_for(read_events(lines, {"status"}), 5)
            assert first["status"]["engine"] == {"running": True}
            put = await c.put("/api/walls/3", json={"grid": 16})
            assert put.status_code == 200
            got = await asyncio.wait_for(read_events(lines, {"config"}), 5)
            assert got["config"]["scope"] == "walls" and got["config"]["revision"] >= 1
    anon = await httpx.AsyncClient(base_url=live_server).get("/api/events")
    assert anon.status_code == 401
