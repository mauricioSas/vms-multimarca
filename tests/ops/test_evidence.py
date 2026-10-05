"""Evidencias y marcadores (CONTRATO §18.6-§18.7, criterios 4 y 5 de B6).

- `python -m vms.ops.evidence verify` acepta el paquete y falla si se cambia UN byte (de un vídeo, del
  manifiesto o de la firma) o si sobra o falta un archivo.
- Un tramo protegido sobrevive al borrado de MediaMTX (enlace duro), se puede seguir exportando y caduca.
- Línea en audit.log con el SHA-256 del manifiesto; motivo obligatorio; permisos de exportación.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.ops.conftest import Harness, new_device, wait_for, write_segments
from vms.engine.disk_guard import parse_segment_name as guard_parse
from vms.ops.evidence import __main__ as cli
from vms.ops.evidence.segments import parse_segment_name
from vms.ops.evidence.verify import verify

T0 = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
MP4 = b"\x00\x00\x00\x18ftypisom" + b"remux-de-prueba" * 64


def _fake_playback(calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.params["format"] == "mp4"
        return httpx.Response(200, content=MP4, headers={"Content-Type": "video/mp4"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _setup(api: Harness) -> tuple[httpx.AsyncClient, str, Path]:
    admin = await api.login()
    cam = (await new_device(admin, import_channels=[1]))["cameras"][0]
    rec = Path(api.state.recordings_dir())
    write_segments(rec, cam, T0, 6, seconds=60, size=20_000)
    return admin, cam, rec


async def _export(c: httpx.AsyncClient, cam: str, **over: Any) -> dict[str, Any]:
    body = {"camera_ids": [cam], "start": (T0 + timedelta(seconds=30)).isoformat(),
            "end": (T0 + timedelta(minutes=3)).isoformat(), "reason": "Robo en caja 2", "case_ref": "AT-2026/123",
            "recipient": "Policía Local", **over}
    r = await c.post("/api/evidence/exports", json=body)
    assert r.status_code == 202, r.text
    exp_id = r.json()["export_id"]

    async def done() -> dict[str, Any] | None:
        e = (await c.get(f"/api/evidence/exports/{exp_id}")).json()
        return e if e["state"] in ("done", "failed") else None
    return await wait_for(done)  # type: ignore[no-any-return]


def test_segment_name_parser_matches_the_disk_guard() -> None:
    for name in ("2026-10-04_10-00-00-000000+0000.mp4", "2026-10-04_12-30-15-123+0200.mp4",
                 "2026-03-29_01-59-59-999999-0100.mp4", "otra-cosa.mp4"):
        assert parse_segment_name(name) == guard_parse(name)


async def test_export_package_verifies_and_detects_a_changed_byte(api: Harness, tmp_path: Path,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    admin, cam, _ = await _setup(api)
    calls: list[str] = []
    api.state.proxy = _fake_playback(calls)
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        exp = await _export(admin, cam)
    assert exp["state"] == "done", exp
    assert exp["export_id"].startswith("ev-") and exp["sha256_manifest"] and calls, exp
    r = await admin.get(exp["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    pkg = tmp_path / f"{exp['export_id']}.zip"
    pkg.write_bytes(r.content)

    res = verify(pkg)
    assert res.ok, res.errors
    m = res.manifest
    assert m is not None and m.reason == "Robo en caja 2" and m.case_ref == "AT-2026/123"
    assert m.recipient == "Policía Local" and m.created_by == "admin"
    paths = {f.path for f in m.files}
    segs = sorted(p for p in paths if "/segments/" in p)
    # 10:00:30 a 10:03:00: los segmentos de 10:00, 10:01 y 10:02 (el de 10:03 empieza justo al final)
    assert [p.rsplit("/", 1)[1][11:16] for p in segs] == ["10-00", "10-01", "10-02"], segs
    assert any(p.endswith(".mp4") and "/segments/" not in p for p in paths), "falta el MP4 unido"
    assert {"visor.html", "acta.html", "LEEME.txt", "clave-publica.pem"} <= paths
    assert m.signing_key["key_id"] == api.state.paths.base.joinpath("ops", "evidence-key.json").read_text() \
        .split('"key_id": "')[1].split('"')[0]
    assert cli.main(["verify", str(pkg)]) == 0

    # audit.log: quién, qué, motivo y huella del manifiesto
    line = next(json.loads(r.getMessage()) for r in caplog.records
                if r.name == "vms.audit" and '"evidence_export"' in r.getMessage())
    assert line["user"] == "admin" and line["cameras"] == [cam] and line["reason"] == "Robo en caja 2"
    assert line["export_id"] == exp["export_id"] and line["sha256_manifest"] == exp["sha256_manifest"]

    # un byte cambiado en un segmento → falla
    tampered = _rewrite(pkg, tmp_path / "t1.zip", lambda n, d: d[:100] + bytes([d[100] ^ 1]) + d[101:]
                        if n.endswith(segs[0].rsplit("/", 1)[1]) else d)
    res = verify(tampered)
    assert not res.ok and any("SHA-256 no coincide" in e for e in res.errors)
    assert cli.main(["verify", str(tampered)]) == 1
    # el manifiesto retocado (aunque se recalcule el hash del archivo) → la firma no vale
    forged = _rewrite(pkg, tmp_path / "t2.zip", lambda n, d: d.replace(b"Robo en caja 2", b"Robo en caja 3")
                      if n.endswith("manifiesto.json") else d)
    res = verify(forged)
    assert not res.ok and any("firma" in e.lower() for e in res.errors)
    # un archivo de más → falla
    extra = _rewrite(pkg, tmp_path / "t3.zip", lambda n, d: d, add=("video/" + cam + "/intruso.mp4", b"x"))
    assert not verify(extra).ok
    # otra instalación (key_id distinto) → falla con --key-id
    assert cli.main(["verify", str(pkg), "--key-id", "00" * 32]) == 1


def _rewrite(src: Path, dst: Path, fn: Any, add: tuple[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w") as zo:
        prefix = zi.namelist()[0].split("/", 1)[0] + "/"
        for info in zi.infolist():
            zo.writestr(info.filename, fn(info.filename, zi.read(info.filename)))
        if add:
            zo.writestr(prefix + add[0], add[1])
    return dst


async def test_export_rules_reason_scope_and_no_recording(api: Harness) -> None:
    admin, cam, _ = await _setup(api)
    r = await admin.post("/api/evidence/exports", json={"camera_ids": [cam], "start": T0.isoformat(),
                                                        "end": (T0 + timedelta(minutes=1)).isoformat(), "reason": ""})
    assert r.status_code == 422, "el motivo es obligatorio"
    exp = await _export(admin, cam, start="2026-01-01T00:00:00Z", end="2026-01-01T00:10:00Z")
    assert exp["state"] == "failed" and "No hay grabación" in exp["error"]
    # operador sin permiso de exportar en esa cámara: 404 (no se revela que existe)
    op = await api.operator()
    r = await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [cam], "export": False}})
    assert r.status_code == 200, r.text
    r = await op.post("/api/evidence/exports", json={"camera_ids": [cam], "start": T0.isoformat(),
                                                     "end": (T0 + timedelta(minutes=1)).isoformat(), "reason": "Hurto"})
    assert r.status_code == 404
    # sus exportaciones no se ven entre usuarios (salvo el administrador)
    assert (await op.get("/api/evidence/exports")).json() == []


async def test_protected_range_survives_retention_and_expires(api: Harness, tmp_path: Path) -> None:
    admin, cam, rec = await _setup(api)
    body = {"camera_id": cam, "start": (T0 + timedelta(seconds=70)).isoformat(),
            "end": (T0 + timedelta(seconds=170)).isoformat(), "note": "Discusión en caja", "protect": True,
            "protect_reason": "Denuncia del cliente", "protect_days": 90, "case_ref": "D-77"}
    r = await admin.post("/api/bookmarks", json=body)
    assert r.status_code == 201, r.text
    bm = r.json()
    assert bm["protected"] and bm["protected_files"] and bm["id"].startswith("bm-")
    folder = api.state.paths.base / "evidence" / "protected" / cam / bm["id"]
    copies = sorted(p for p in folder.glob("*.mp4"))
    assert len(copies) == 2, copies
    originals = [rec / cam / "main" / p.name for p in copies]
    assert all(o.stat().st_nlink >= 2 for o in originals), "se esperaba un enlace duro"
    data_before = [p.read_bytes() for p in copies]
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["reason"] == "Denuncia del cliente" and meta["user"] == "admin"

    for o in originals:   # la retención de MediaMTX borra los originales…
        o.unlink()
    assert [p.read_bytes() for p in copies] == data_before   # …pero la copia protegida sigue
    api.state.proxy = _fake_playback([])
    exp = await _export(admin, cam, start=(T0 + timedelta(seconds=80)).isoformat(),
                        end=(T0 + timedelta(seconds=150)).isoformat(), include_mp4=False)
    assert exp["state"] == "done", exp
    pkg = tmp_path / "p.zip"
    pkg.write_bytes((await admin.get(exp["download_url"])).content)
    res = verify(pkg)
    assert res.ok and res.manifest is not None
    assert {f.path.rsplit("/", 1)[1] for f in res.manifest.files if f.kind == "segment"} == {p.name for p in copies}

    # aparece en la línea de tiempo
    tl = (await admin.get(f"/api/timeline/{cam}", params={"start": T0.isoformat(),
                                                          "end": (T0 + timedelta(hours=1)).isoformat()})).json()
    assert {"bookmark", "protected"} <= {e["layer"] for e in tl}

    # caduca: el mantenimiento borra la copia y lo anota
    ops = api.app.state.ops
    ops.bookmarks.clock = lambda: datetime.now(timezone.utc) + timedelta(days=91)
    await ops.maintenance()
    assert not folder.exists()
    after = (await admin.get("/api/bookmarks", params={"camera_id": cam})).json()[0]
    assert after["protected"] is False and after["release_reason"] == "caducada" and after["released_at"]


async def test_protect_requires_reason_and_permission(api: Harness) -> None:
    admin, cam, _ = await _setup(api)
    body = {"camera_id": cam, "start": T0.isoformat(), "end": (T0 + timedelta(seconds=60)).isoformat(), "protect": True}
    r = await admin.post("/api/bookmarks", json=body)
    assert r.status_code == 422 and "motivo" in r.json()["error"]["message"]
    op = await api.operator()
    await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [cam], "bookmark": True, "export": False}})
    r = await op.post("/api/bookmarks", json={**body, "protect_reason": "Prueba de permisos"})
    assert r.status_code == 403
    r = await op.post("/api/bookmarks", json={**body, "protect": False, "note": "Punto de interés"})
    assert r.status_code == 201
    bm = r.json()["id"]
    assert (await op.delete(f"/api/bookmarks/{bm}")).status_code == 403        # borrar: solo administrador
    r = await admin.patch(f"/api/bookmarks/{bm}", json={"protect": True, "protect_reason": "Lo pide la policía"})
    assert r.status_code == 200 and r.json()["protected"]
    r = await admin.patch(f"/api/bookmarks/{bm}", json={"protect": False})
    assert r.status_code == 422, "desproteger exige motivo"
    r = await admin.patch(f"/api/bookmarks/{bm}", json={"protect": False, "protect_reason": "Caso archivado"})
    assert r.status_code == 200 and not r.json()["protected"]
    assert (await admin.delete(f"/api/bookmarks/{bm}")).status_code == 204


def test_signature_is_over_exact_manifest_bytes(tmp_path: Path) -> None:
    """La firma es Ed25519 sobre los bytes EXACTOS del manifiesto (no sobre un JSON re-serializado)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from vms.core.paths import AppPaths
    from vms.ops.evidence.keys import load_or_create, read_public

    paths = AppPaths(tmp_path).ensure()
    key = load_or_create(paths)
    assert load_or_create(paths).key_id == key.key_id, "la clave se crea una vez y se reutiliza"
    data = b'{"a": 1}\n'
    sig = key.sign(data)
    Ed25519PublicKey.from_public_bytes(base64.b64decode(read_public(paths)["public_key"])).verify(sig, data)  # type: ignore[index]
    assert (paths.secrets_dir / "evidence-ed25519.key").stat().st_mode & 0o077 == 0
    assert io.BytesIO(key.public_pem().encode()).read().startswith(b"-----BEGIN PUBLIC KEY-----")
