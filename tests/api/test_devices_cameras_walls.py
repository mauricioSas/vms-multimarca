"""Equipos, cámaras, muros y snapshot (CONTRATO §6.3-6.5)."""
from __future__ import annotations

from urllib.parse import quote

from tests.api.conftest import Harness, new_device
from vms.core.interfaces import DiscoveredDevice, PathStatus


async def test_create_device_stores_password_only_in_credential_store(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, password="S3cr#t:@/x")
    assert dev["has_password"] is True and dev["cameras"] == [] and "password" not in dev
    assert dev["id"].startswith("dev-")
    config_text = api.state.paths.config_file.read_text(encoding="utf-8")
    assert "S3cr#t" not in config_text and "S3cr%23t" not in config_text
    assert api.creds.get_device_password(dev["id"]) == "S3cr#t:@/x"
    listed = (await admin.get("/api/devices")).json()
    assert [d["id"] for d in listed] == [dev["id"]] and "S3cr" not in str(listed)
    # misma IP y puertos → 409
    r = await admin.post("/api/devices", json={"name": "Otro", "vendor": "dahua", "host": "10.0.0.5"})
    assert r.status_code == 409


async def test_create_with_import_channels_and_engine_receives_sources(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels="all", password="Cl@ve#1")
    assert dev["model"] == "FAKE-NVR-8" and dev["serial"] == "FAKE123" and len(dev["cameras"]) == 4
    assert api.client_calls[-1] == (dev["id"], "Cl@ve#1")
    cams = (await admin.get("/api/cameras")).json()
    assert [c["name"] for c in cams] == ["Cámara 1", "Cámara 2", "Cámara 3", "Cámara 4"]
    assert all(c["vendor"] == "hikvision" and c["device_name"] == "NVR Tienda" for c in cams)
    await api.settle()
    src = api.engine.sources[cams[0]["id"]]
    assert src.main_url == f"rtsp://admin:{quote('Cl@ve#1', safe='')}@10.0.0.5:554/Streaming/Channels/101"
    assert src.sub_url and src.sub_url.endswith("/Streaming/Channels/102")
    cams = (await admin.get("/api/cameras")).json()
    assert cams[0]["live"]["online"] is True and cams[0]["live"]["recording"] is True
    assert (await admin.get(f"/api/devices/{dev['id']}")).json()["online"] is True

    # importar de nuevo: solo los que faltan
    r = await admin.post(f"/api/devices/{dev['id']}/channels/import", json={"channels": "all"})
    assert r.status_code == 201 and r.json() == []
    r = await admin.post(f"/api/devices/{dev['id']}/channels/import", json={"channels": [9]})
    assert r.status_code == 422


async def test_import_failure_still_creates_device(api: Harness) -> None:
    admin = await api.login()
    api.behavior["reachable"] = False
    r = await admin.post("/api/devices", json={"name": "NVR caído", "vendor": "dahua", "host": "10.0.0.7",
                                                "import_channels": [1, 2]})
    assert r.status_code == 201
    body = r.json()
    assert body["cameras"] == [] and "no responde" in body["details"]["import_error"]
    r = await admin.get(f"/api/devices/{body['id']}/channels")
    assert r.status_code == 502 and r.json()["error"]["code"] == "device_unreachable"
    api.behavior.update(reachable=True, password_ok=False)
    r = await admin.get(f"/api/devices/{body['id']}/channels")
    assert r.status_code == 502 and r.json()["error"]["code"] == "device_auth_failed"
    api.behavior["password_ok"] = True
    r = await admin.post(f"/api/devices/{body['id']}/channels/import", json={"channels": [1, 2]})
    assert r.status_code == 201 and [c["channel"] for c in r.json()] == [1, 2]


async def test_patch_device_password_semantics_and_reapply(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1])
    await api.settle()
    calls = api.engine.apply_calls
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"name": "NVR renombrado", "password": None})
    assert r.status_code == 200 and r.json()["name"] == "NVR renombrado" and r.json()["has_password"]
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"password": "Nueva#2"})
    assert r.status_code == 200
    await api.settle()
    assert api.engine.apply_calls > calls
    cam_id = dev["cameras"][0]
    assert "Nueva%232@" in api.engine.sources[cam_id].main_url
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"password": ""})
    assert r.json()["has_password"] is False and api.creds.get_device_password(dev["id"]) == ""
    r = await admin.patch(f"/api/devices/{dev['id']}", json={"host": "10.0.0.200", "rtsp_port": 8554})
    await api.settle()
    assert r.status_code == 200 and "10.0.0.200:8554" in api.engine.sources[cam_id].main_url
    assert (await admin.patch(f"/api/devices/{dev['id']}", json={"host": "mal host!"})).status_code == 422
    assert (await admin.patch("/api/devices/dev-00000000", json={"name": "x"})).status_code == 404


async def test_delete_device_cascades(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels="all")
    cam = dev["cameras"][0]
    assert (await admin.put("/api/walls/1", json={"grid": 4, "cells": [cam]})).status_code == 200
    r = await admin.post("/api/analytics/rules", json={"kind": "line", "camera_id": cam, "name": "Entrada",
                                                        "start": [0.1, 0.5], "end": [0.9, 0.5]})
    assert r.status_code == 201
    assert (await admin.delete(f"/api/devices/{dev['id']}")).status_code == 204
    assert (await admin.get("/api/cameras")).json() == []
    assert (await admin.get("/api/walls/1")).json()["cells"][0] is None
    assert (await admin.get("/api/analytics/rules")).json() == []
    assert api.creds.get_device_password(dev["id"]) == ""
    await api.settle()
    assert api.engine.sources == {}
    assert (await admin.delete(f"/api/devices/{dev['id']}")).status_code == 404


async def test_device_test_endpoints_and_discovery(api: Harness) -> None:
    admin = await api.login()
    r = await admin.post("/api/devices/test", json={"vendor": "hikvision", "host": "10.0.0.9", "password": "buena"})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = await admin.post("/api/devices/test", json={"vendor": "hikvision", "host": "10.0.0.9", "password": "mala"})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["auth_ok"] is False
    dev = await new_device(admin, password="buena")
    r = await admin.post(f"/api/devices/{dev['id']}/test")
    assert r.json()["ok"] is True and api.tester_calls[-1] == ("10.0.0.5", "buena")

    api.discovered = [DiscoveredDevice(host="10.0.0.5", vendor_guess="hikvision"),
                      DiscoveredDevice(host="10.0.0.99", vendor_guess="dahua")]
    r = await admin.post("/api/discovery/scan", json={"timeout_s": 1})
    found = {d["host"]: d["already_added"] for d in r.json()["devices"]}
    assert found == {"10.0.0.5": True, "10.0.0.99": False}
    assert (await admin.post("/api/discovery/scan", json={"timeout_s": 60})).status_code == 422
    op = await api.operator()
    assert (await op.post("/api/discovery/scan", json={})).status_code == 403


async def test_camera_crud_and_validation(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin)
    r = await admin.post("/api/cameras", json={"name": "Puerta", "device_id": dev["id"], "channel": 2})
    assert r.status_code == 201
    cam = r.json()
    assert cam["main_path"] is None and cam["live"]["online"] in (None, False)
    r = await admin.post("/api/cameras", json={"name": "Otra", "device_id": dev["id"], "channel": 2})
    assert r.status_code == 409
    r = await admin.post("/api/cameras", json={"name": "X", "device_id": "dev-ffffffff", "channel": 1})
    assert r.status_code == 422
    onvif = await new_device(admin, name="Cámara ONVIF", vendor="onvif", kind="camera", host="10.0.0.40")
    r = await admin.post("/api/cameras", json={"name": "Sin ruta", "device_id": onvif["id"]})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["loc"] == ["main_path"]
    r = await admin.post("/api/cameras", json={"name": "Con ruta", "device_id": onvif["id"],
                                                "main_path": "Streaming/Channels/101", "has_sub": False})
    assert r.status_code == 201 and r.json()["main_path"] == "/Streaming/Channels/101"

    r = await admin.patch(f"/api/cameras/{cam['id']}", json={"name": "Puerta principal", "record": False,
                                                              "main_path": "/manual/main"})
    assert r.status_code == 200 and r.json()["record"] is False and r.json()["main_path"] == "/manual/main"
    r = await admin.patch(f"/api/cameras/{cam['id']}", json={"main_path": None})
    assert r.json()["main_path"] is None  # vuelve al preset
    await api.settle()
    assert api.engine.sources[cam["id"]].record is False
    op = await api.operator()
    assert (await op.patch(f"/api/cameras/{cam['id']}", json={"name": "x"})).status_code == 403
    assert (await op.get(f"/api/cameras/{cam['id']}")).status_code == 200
    assert (await admin.delete(f"/api/cameras/{cam['id']}")).status_code == 204
    assert (await admin.get(f"/api/cameras/{cam['id']}")).status_code == 404


async def test_codec_warning_for_h265_substream(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1])
    cam_id = dev["cameras"][0]
    await api.settle()
    original = api.engine.paths_status

    async def h265() -> dict[str, PathStatus]:
        data = await original()
        data[f"{cam_id}/sub"] = data[f"{cam_id}/sub"].model_copy(update={"tracks": ["H265"]})
        return data

    api.engine.paths_status = h265  # type: ignore[method-assign]
    api.state._paths_cache = None
    cam = (await admin.get(f"/api/cameras/{cam_id}")).json()
    assert "H.265" in cam["live"]["codec_warning"] and "H.264" in cam["live"]["codec_warning"]


async def test_snapshot_in_memory_with_rate_limit(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1])
    cam_id = dev["cameras"][0]
    r = await admin.get(f"/api/cameras/{cam_id}/snapshot", params={"stream": "sub"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert "no-store" in r.headers["cache-control"]
    r = await admin.get(f"/api/cameras/{cam_id}/snapshot")
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert not list(api.state.paths.base.rglob("*.jpg")), "el snapshot nunca se escribe en disco"
    op = await api.operator()
    assert (await op.get(f"/api/cameras/{cam_id}/snapshot")).status_code == 403


async def test_walls(api: Harness) -> None:
    admin = await api.login()
    dev = await new_device(admin, import_channels="all")
    c1, c2 = dev["cameras"][:2]
    walls = (await admin.get("/api/walls")).json()
    assert [w["monitor"] for w in walls] == [1, 2, 3, 4] and all(len(w["cells"]) == 16 for w in walls)
    op = await api.operator()
    r = await op.put("/api/walls/2", json={"name": "Cajas", "grid": 9, "cells": [c1, None, c2]})
    assert r.status_code == 200
    wall = r.json()
    assert wall["grid"] == 9 and wall["cells"][:3] == [c1, None, c2] and len(wall["cells"]) == 16
    r = await op.get("/api/walls/2")
    etag = r.headers["etag"]
    assert (await op.get("/api/walls/2", headers={"If-None-Match": etag})).status_code == 304
    r = await op.put("/api/walls/2", json={"cells": ["cam-00000000"]})
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["loc"] == ["cells", 0]
    assert (await op.put("/api/walls/2", json={"grid": 5})).status_code == 422
    assert (await op.get("/api/walls/5")).status_code == 404
    r = await op.put("/api/walls/2", json={"grid": 1})
    assert r.json()["cells"][:3] == [c1, None, c2], "cambiar la cuadrícula no pierde asignaciones"
