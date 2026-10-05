"""Pruebas de la revisión de seguridad v2 (solo lectura del repo). Se ejecutan con:
    cd <worktree> && .venv/bin/python -m pytest -p tests.conftest -p no:randomly <este archivo>
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import httpx

from tests.ops.conftest import Harness, api, new_device  # noqa: F401
from tests.ops.test_evidence import _export, _fake_playback, _setup as _ev_setup
from vms.ops.evidence.verify import verify

VMSCTL = Path(__file__).parent / "target" / "debug" / "vmsctl"
OLD = "token-kiosco-de-pruebas"   # el de tests/conftest.py (= VMS_KIOSK_TOKEN del .env)


# ------------------------------------------------------------------ 1. kiosk.token no es el que valida el backend
async def test_kiosk_rotate_is_ignored_by_backend(api: Harness) -> None:  # noqa: F811
    data = Path(api.state.paths.base)
    out = subprocess.run([str(VMSCTL), "kiosk", "rotate", "--data-dir", str(data), "--json"],
                         capture_output=True, text=True, check=True)
    print("vmsctl:", out.stdout.strip())
    new = (data / "secrets" / "kiosk.token").read_text().strip()
    assert new and new != OLD
    c = api.client()
    # 1) el token del archivo (lo que lee el visor) NO vale
    r = await c.post("/api/local/kiosk-session", json={"token": new, "next": "/wall/1"})
    print("token nuevo de kiosk.token ->", r.status_code, r.text)
    assert r.status_code == 401
    # 2) el token «revocado» (el de antes de rotar) sigue valiendo
    r = await api.client().post("/api/local/kiosk-session", json={"token": OLD, "next": "/wall/1"})
    print("token anterior ->", r.status_code)
    assert r.status_code == 204


async def test_kiosk_without_env_token_is_disabled_even_with_file(api: Harness) -> None:  # noqa: F811
    data = Path(api.state.paths.base)
    subprocess.run([str(VMSCTL), "kiosk", "rotate", "--data-dir", str(data)], capture_output=True, check=True)
    api.state.settings = api.state.settings.model_copy(update={"kiosk_token": None})
    new = (data / "secrets" / "kiosk.token").read_text().strip()
    r = await api.client().post("/api/local/kiosk-session", json={"token": new, "next": "/wall/1"})
    print("sin VMS_KIOSK_TOKEN, con kiosk.token ->", r.status_code, r.text)
    assert r.status_code == 403 and r.json()["error"]["code"] == "kiosk_disabled"


# ------------------------------------------------------------------ 2. webhook hacia loopback / LAN
async def test_webhook_ssrf_to_loopback(api: Harness) -> None:  # noqa: F811
    hits: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        hits.append(head.decode(errors="replace").splitlines()[0])
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    admin = await api.login()
    cur = (await admin.get("/api/notifications/settings")).json()
    cur.update(webhook_enabled=True, webhook_url=f"http://127.0.0.1:{port}/v3/config/global/patch")
    r = await admin.put("/api/notifications/settings", json=cur)
    assert r.status_code == 200, r.text
    assert (await admin.put("/api/notifications/secrets", json={"generate_webhook_secret": True})).status_code == 200
    rec = (await admin.post("/api/notifications/test", json={"channel": "webhook"})).json()
    # puerto cerrado: otro mensaje de error (oráculo para barrer puertos)
    cur["webhook_url"] = "http://127.0.0.1:1/"
    await admin.put("/api/notifications/settings", json=cur)
    rec2 = (await admin.post("/api/notifications/test", json={"channel": "webhook"})).json()
    cur["webhook_url"] = "http://169.254.169.254/latest/meta-data/"
    r3 = await admin.put("/api/notifications/settings", json=cur)
    srv.close()
    print("petición recibida en loopback:", hits)
    print("error con puerto abierto:", rec["error"], "| con puerto cerrado:", rec2["error"])
    print("URL de metadatos aceptada:", r3.status_code)
    assert hits and hits[0].startswith("POST /v3/config/global/patch")
    assert rec["error"] != rec2["error"] and r3.status_code == 200


# ------------------------------------------------------------------ 3. ámbito por cámara: ids fuera del ámbito
async def test_scope_leaks_camera_ids(api: Harness) -> None:  # noqa: F811
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    op = await api.operator()
    r = await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [c1], "live": True,
                                                                        "playback": True, "export": False}})
    assert r.status_code == 200
    assert (await op.get(f"/api/cameras/{c2}")).status_code == 404    # «no se revela que existe»
    fc = (await op.get("/api/retention-forecast")).json()
    ids = [c["camera_id"] for c in fc["cameras"]]
    print("retention-forecast (operador con ámbito solo", c1, ") ->", ids)
    assert c2 in ids
    from vms.api.routes.events import status_event
    ev = (await status_event(api.state)).decode()
    print("evento SSE status (igual para todos):", ev.strip()[:200])
    assert c2 in ev


# ------------------------------------------------------------------ 4. evidencias: zip con nombres repetidos
async def test_evidence_zip_with_duplicate_entry_verifies(api: Harness, tmp_path: Path) -> None:  # noqa: F811
    admin, cam, _ = await _ev_setup(api)
    api.state.proxy = _fake_playback([])
    exp = await _export(admin, cam)
    pkg = tmp_path / "orig.zip"
    pkg.write_bytes((await admin.get(exp["download_url"])).content)
    assert verify(pkg).ok
    zin = zipfile.ZipFile(pkg)
    video = next(n for n in zin.namelist() if n.endswith(".mp4"))
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(forged, "w") as zout:
        zout.writestr(video, b"VIDEO FALSO que ve quien abre el zip con el Explorador")   # primera entrada
        for info in zin.infolist():
            zout.writestr(info, zin.read(info))
    res = verify(forged)
    names = zipfile.ZipFile(forged).namelist()
    print("entradas con el mismo nombre:", names.count(video), "| verify.ok =", res.ok, res.errors)
    first = zipfile.ZipFile(forged).open(zipfile.ZipFile(forged).infolist()[0]).read()
    print("contenido de la primera entrada:", first[:40])
    assert res.ok and names.count(video) == 2
