"""Salud por la API, informe diario, latido y RGPD (CONTRATO §18.2-§18.5 y §18.17; criterios 1, 11 y 12 de B6).

Las imágenes de la cámara se sustituyen por fotogramas SINTÉTICOS (`synthetic.py`). Tras una ejecución
completa (referencia, comprobaciones, aviso de sabotaje, recuperación, informe, latido), la carpeta `ops/`
solo contiene referencias de cámara y metadatos.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from tests.ops import synthetic as syn
from tests.ops.conftest import Harness, new_device, wait_for
from vms.ops import heartbeat as hb
from vms.ops import service as service_mod


class FrameFeed:
    """Sustituye a la cámara: devuelve fotogramas sintéticos («normal», «tapada»…) en el orden pedido."""

    def __init__(self) -> None:
        self.base = syn.scene(1)
        self.mode = "normal"
        self.i = 0
        self.served: list[np.ndarray] = []

    async def __call__(self, state: Any, cam: Any, backoff: Any = None) -> np.ndarray:
        self.i += 1
        img = syn.normal_frame(self.base, self.i) if self.mode == "normal" else syn.alterations(self.base)[self.mode]
        self.served.append(img)
        return img


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> FrameFeed:
    f = FrameFeed()
    monkeypatch.setattr(service_mod, "grab", f)
    return f


async def _reference(admin: Any, ops: Any, cam: str, kind: str = "day") -> dict[str, Any]:
    ops.reference_interval_s = 0.0
    r = await admin.post(f"/api/camera-health/{cam}/reference", json={"kind": kind})
    assert r.status_code == 202, r.text
    job = r.json()["job_id"]

    async def done() -> dict[str, Any] | None:
        j = (await admin.get(f"/api/camera-health/jobs/{job}")).json()
        return j if j["state"] != "running" else None
    return await wait_for(done)  # type: ignore[no-any-return]


async def test_reference_check_hysteresis_alert_and_report(api: Harness, feed: FrameFeed,
                                                           caplog: pytest.LogCaptureFixture) -> None:
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    ops = api.app.state.ops
    events = api.state.bus.subscribe()
    h = (await admin.get(f"/api/camera-health/{c1}")).json()
    assert h["status"] == "unknown" and h["causes"] == ["no_reference"]

    with caplog.at_level(logging.INFO, logger="vms.audit"):
        job = await _reference(admin, ops, c1)
        assert job["state"] == "done" and job["frames"] == 15 and not job["person_warning"], job
        r = await admin.get(f"/api/camera-health/{c1}/reference.jpg", params={"kind": "day"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
    assert any('"health_reference_view"' in rec.getMessage() for rec in caplog.records), "ver la referencia se audita"
    op = await api.operator()
    assert (await op.get(f"/api/camera-health/{c1}/reference.jpg")).status_code == 403

    chk = (await admin.post(f"/api/camera-health/{c1}/check")).json()
    assert chk["status"] == "ok" and chk["score"] == 100 and chk["duration_ms"] < 100
    feed.mode = "covered"
    for _ in range(2):
        assert (await admin.post(f"/api/camera-health/{c1}/check")).json()["causes"] == ["covered"]
    assert (await admin.get(f"/api/camera-health/{c1}")).json()["status"] == "ok", "histéresis: aún no cambia"
    await admin.post(f"/api/camera-health/{c1}/check")
    h = (await admin.get(f"/api/camera-health/{c1}")).json()
    assert h["status"] == "critical" and h["score"] == 0 and h["causes"] == ["covered"] and h["references"]["day"]

    got: dict[str, list[dict[str, Any]]] = {}
    while not events.empty():
        name, data = events.get_nowait()
        got.setdefault(name, []).append(json.loads(data))
    assert any(e["status"] == "critical" and e["causes"] == ["covered"] for e in got["health"])
    assert any(n["kind"] == "tamper" and n["severity"] == "critical" for n in got["notice"])
    alert_img = api.state.paths.base / "ops" / "references" / f"{c1}-alert.jpg"
    assert alert_img.is_file(), "se guarda la imagen del aviso mientras está abierto"

    rep = (await admin.get("/api/health-report")).json()
    row = next(c for c in rep["cameras"] if c["camera_id"] == c1)
    assert row["health_score_min"] == 0 and row["health_causes"] == ["covered"]
    assert rep["status"] == "critical" and any("Cámara tapada" in p or "cámara tapada" in p for p in rep["problems"])
    csv = (await admin.get("/api/health-report.csv")).content
    assert csv.startswith(b"\xef\xbb\xbf") and b";" in csv.splitlines()[0]
    tl = (await admin.get(f"/api/timeline/{c1}", params={"start": rep["generated_at"][:10] + "T00:00:00Z",
                                                          "end": rep["generated_at"][:10] + "T23:59:59Z",
                                                          "layers": "health"})).json()
    assert tl and tl[0]["layer"] == "health" and tl[0]["severity"] == "critical"

    # recuperación: tres comprobaciones buenas, se cierra el aviso y se borra su imagen
    feed.mode = "normal"
    for _ in range(3):
        await admin.post(f"/api/camera-health/{c1}/check")
    assert (await admin.get(f"/api/camera-health/{c1}")).json()["status"] == "ok"
    assert not alert_img.exists()

    # latido: payload.health y la clave de evidencias, sin imágenes ni IP
    summary = await ops.refresh_summary()
    ops.key()
    payload = await api.state.heartbeat_payload()
    assert payload["health"] == summary and set(summary) == {"report_date", "status", "score_min", "cameras_critical",
                                                             "cameras_warning", "clock_worst_s", "forecast_days",
                                                             "problems"}
    assert len(summary["problems"]) <= 10 and summary["score_min"] == 0
    assert payload["evidence_key"]["key_id"] == ops.key().key_id
    blob = json.dumps(payload)
    assert "10.0.0.5" not in blob and "jpg" not in blob and "base64" not in blob.lower()
    assert hb.payload_health(api.state.paths) == summary

    # RGPD: en ops/ solo hay referencias de cámara y metadatos
    _assert_ops_only_references_and_metadata(api.state.paths.base / "ops", feed)
    # al borrar la cámara, su referencia se borra en el siguiente mantenimiento
    assert (await admin.delete(f"/api/cameras/{c1}")).status_code == 204
    await ops.maintenance()
    assert not list((api.state.paths.base / "ops" / "references").glob(f"{c1}*"))


def _assert_ops_only_references_and_metadata(ops_dir: Path, feed: FrameFeed) -> None:
    allowed_top = {"ops.sqlite3", "ops.sqlite3-wal", "ops.sqlite3-shm", "onboarding.json", "health-summary.json",
                   "evidence-key.json"}
    for p in ops_dir.rglob("*"):
        if p.is_dir():
            assert p.name in ("references", "advisories"), p
            continue
        rel = p.relative_to(ops_dir).as_posix()
        data = p.read_bytes()
        is_image = data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
        if rel.startswith("references/"):
            assert p.suffix in (".jpg", ".json"), rel
            if p.suffix == ".jpg":
                assert p.stem.rsplit("-", 1)[1] in ("day", "night", "alert"), rel
                if p.stem.endswith("-day"):
                    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    # la referencia es una mediana, no ninguno de los fotogramas que se tomaron
                    for f in feed.served[:15]:
                        assert float(cv2.absdiff(img, cv2.resize(f, (img.shape[1], img.shape[0]))).mean()) > 0.5
        else:
            assert rel in allowed_top, f"archivo inesperado en ops/: {rel}"
            assert not is_image, f"imagen fuera de references/: {rel}"
            if rel == "ops.sqlite3":
                assert b"\xff\xd8\xff\xe0" not in data, "la base no guarda imágenes"


async def test_health_config_masks_and_clock_and_forecast_routes(api: Harness, feed: FrameFeed) -> None:
    admin = await api.login()
    cam = (await new_device(admin, import_channels=[1]))["cameras"][0]
    r = await admin.put(f"/api/camera-health/{cam}/config",
                        json={"enabled": True, "masks": [[[0, 0], [0.2, 0], [0.2, 0.1]]]})
    assert r.status_code == 200 and len(r.json()["masks"]) == 1
    assert (await admin.put(f"/api/camera-health/{cam}/config", json={"masks": [[[0, 0], [2, 0], [0, 1]]]})
            ).status_code == 422
    clk = (await admin.post("/api/clock/check")).json()
    assert clk["pc"]["status"] in ("ok", "warning", "unknown") and clk["devices"][0]["status"] == "unknown"
    assert "no sabe leer la hora" in clk["devices"][0]["message_es"], "FakeDeviceClient no tiene time_read"
    assert (await admin.get("/api/clock")).json()["devices"]
    fc = (await admin.get("/api/retention-forecast")).json()
    assert fc["target_days"] == 30 and "message_es" in fc
    sim = (await admin.post("/api/retention-forecast/simulate", json={"add_cameras": 4, "bitrate_mbps": 4})).json()
    assert sim["target_days"] == 30
    op = await api.operator()
    assert (await op.post("/api/retention-forecast/simulate", json={})).status_code == 403
    assert (await admin.get("/api/health-report", params={"date": "ayer"})).status_code == 422


async def test_now_jpg_is_not_stored(api: Harness, feed: FrameFeed) -> None:
    admin = await api.login()
    cam = (await new_device(admin, import_channels=[1]))["cameras"][0]
    r = await admin.get(f"/api/camera-health/{cam}/now.jpg")
    assert r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff"
    refs = api.state.paths.base / "ops" / "references"
    assert not list(refs.glob("*.jpg")), "«Ahora» vive solo en memoria"


async def test_background_service_starts_and_stops_cleanly(api: Harness) -> None:
    ops = api.app.state.ops
    assert ops._tasks and all(not t.done() for t in ops._tasks)
    await asyncio.sleep(0)
