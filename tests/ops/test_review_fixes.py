"""Regresiones de la revisión de B6: cada prueba reproduce un hallazgo del revisor y comprueba la corrección.

(La firma re-firmada está en test_evidence.py y test_evidence_viewer.py; Dahua y los avisos sin versión
corregida, en test_security_audit.py; el paquete instalable, en test_packaging.py.)
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.ops.conftest import Harness, new_device, wait_for, write_segments
from vms.core.errors import DeviceAuthFailed
from vms.core.models import NotificationRule, NotificationSettings, Site
from vms.ops.csvsafe import text_cell
from vms.ops.evidence.bookmarks import BookmarkManager
from vms.ops.evidence.export import EvidenceBuilder, NotEnoughSpace
from vms.ops.health import clock as clockmod
from vms.ops.health.snapshot import AuthBackoff, grab
from vms.ops.models import (Bookmark, CameraDayReport, EvidenceExport, EvidenceExportRequest, HealthCheck,
                            HealthReport)
from vms.ops.notify import Alert, Notifier
from vms.ops.report import report_csv
from vms.ops.store import OpsStore

T0 = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)


# =========================================================================== informe: puntuación sostenida
def _check(cid: str, at: datetime, score: int, causes: list[str] | None = None) -> HealthCheck:
    status = "ok" if score >= 80 else "warning" if score >= 50 else "critical"
    return HealthCheck(camera_id=cid, at=at, score=score, status=status, causes=causes or [])


def test_one_loose_bad_check_does_not_make_the_day_critical(tmp_path: Path) -> None:
    """Hallazgo: 200 comprobaciones a 100 y UNA a 0 marcaban el día como «grave» en la central."""
    store = OpsStore(tmp_path / "ops.sqlite3")
    day = T0.replace(hour=0)
    for i in range(200):
        store.add_health_check(_check("cam-a", day + timedelta(minutes=3 * i), 0 if i == 100 else 100,
                                      ["covered"] if i == 100 else []))
    # cam-b: tapada de verdad 3 comprobaciones seguidas (lo que consolida la histéresis)
    for i in range(50):
        bad = 20 <= i < 23
        store.add_health_check(_check("cam-b", day + timedelta(minutes=3 * i), 0 if bad else 95,
                                      ["covered"] if bad else []))
    # cam-c: 2 malas seguidas → no se sostiene con histéresis 3
    for i in range(50):
        bad = i in (5, 6)
        store.add_health_check(_check("cam-c", day + timedelta(minutes=3 * i), 10 if bad else 90))
    scores = store.health_day_scores(day, day + timedelta(days=1), run=3)
    assert scores["cam-a"] == (100, [])
    assert scores["cam-b"] == (0, ["covered"])
    assert scores["cam-c"][0] == 90
    # con histéresis 1 (la mínima) sí cuenta la suelta
    assert store.health_day_scores(day, day + timedelta(days=1), run=1)["cam-a"] == (0, ["covered"])
    # menos comprobaciones que la histéresis: la mejor de ellas
    store.add_health_check(_check("cam-d", day, 0))
    store.add_health_check(_check("cam-d", day + timedelta(minutes=3), 70))
    assert store.health_day_scores(day, day + timedelta(days=1), run=3)["cam-d"][0] == 70
    store.close()


async def test_report_and_heartbeat_use_the_sustained_score(api: Harness) -> None:
    admin = await api.login()
    cam = (await new_device(admin, import_channels=[1]))["cameras"][0]
    ops = api.app.state.ops
    now = datetime.now(timezone.utc)
    for i in range(30):
        ops.store.add_health_check(_check(cam, now - timedelta(seconds=30 - i), 0 if i == 10 else 100,
                                          ["covered"] if i == 10 else []))
    report = await ops.report()
    row = next(c for c in report.cameras if c.camera_id == cam)
    assert row.health_score_min == 100
    assert not any("tapada" in p for p in report.problems), report.problems
    summary = await ops.refresh_summary()
    assert summary["score_min"] == 100 and not any("tapada" in p for p in summary["problems"])


# =========================================================================== avisos
def _notifier(tmp_path: Path, rules: list[NotificationRule], sent: list[tuple[str, bytes]],
              notices: list[dict[str, Any]] | None = None) -> Notifier:
    from vms.core.credentials import CredentialStore

    class _Creds:
        class backend:  # noqa: N801 - imita CredentialStore.backend
            @staticmethod
            def get(key: str) -> str | None:
                return "secreto-webhook" if key == "notify:webhook_secret" else None

    async def webhook(url: str, body: bytes, headers: dict[str, str]) -> None:
        sent.append((headers["X-VMS-Event"], body))

    settings = NotificationSettings(webhook_enabled=True, webhook_url="http://127.0.0.1:9/hook", rules=rules)
    creds: CredentialStore = _Creds()  # type: ignore[assignment]
    return Notifier(OpsStore(tmp_path / "ops.sqlite3"), creds, lambda: settings, lambda: Site(name="Tienda 37"),
                    webhook_sender=webhook, publish_notice=(notices.append if notices is not None else None))


async def test_default_rule_sends_recoveries_of_alerts_it_sent(tmp_path: Path) -> None:
    """Hallazgo: con la regla por defecto (gravedad mínima «warning») llegaba «Cámara sin vídeo» pero nunca
    «Cámara recuperada» (es «info»)."""
    sent: list[tuple[str, bytes]] = []
    notices: list[dict[str, Any]] = []
    rule = NotificationRule(channels=["webhook"], group_seconds=0)          # la de por defecto, con un canal
    n = _notifier(tmp_path, [rule], sent, notices)
    await n.emit(Alert(kind="camera_down", severity="critical", title_es="Cámara sin vídeo: Cajas 2",
                       camera_id="cam-00000001", camera_name="Cajas 2"))
    await n.emit(Alert(kind="camera_up", severity="info", title_es="Cámara recuperada: Cajas 2",
                       camera_id="cam-00000001", camera_name="Cajas 2"))
    # una recuperación de algo que NO se avisó no sale (sigue mandando la gravedad mínima)
    await n.emit(Alert(kind="camera_up", severity="info", title_es="Cámara recuperada: Pasillo",
                       camera_id="cam-00000002", camera_name="Pasillo"))
    # y la misma recuperación no se repite
    await n.emit(Alert(kind="camera_up", severity="info", title_es="Cámara recuperada: Cajas 2",
                       camera_id="cam-00000001", camera_name="Cajas 2"))
    assert [k for k, _ in sent] == ["camera_down", "camera_up"]
    # los avisos SSE llevan la cámara (para filtrar por ámbito)
    assert notices[0]["camera_ids"] == ["cam-00000001"]
    await n.close()


async def test_disk_and_recording_gap_alerts_are_emitted(api: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hallazgo: la regla por defecto incluye «disk» y «recording_gap», pero nadie los emitía."""
    admin = await api.login()
    cam = (await new_device(admin, import_channels=[1]))["cameras"][0]
    ops = api.app.state.ops
    emitted: list[Alert] = []

    async def emit(alert: Alert) -> None:
        emitted.append(alert)
    monkeypatch.setattr(ops.notifier, "emit", emit)
    # disco: SMART con fallos y casi lleno → avisos «disk» (uno por motivo y día)
    (ops.ops_dir / "smart.json").write_text(json.dumps({"healthy": False}), encoding="utf-8")
    await ops._disk_alerts({"smart_ok": 0.0, "free_gb": 1.2, "percent": 99.0})
    await ops._disk_alerts({"smart_ok": 0.0, "free_gb": 1.2, "percent": 99.0})
    disk = [a for a in emitted if a.kind == "disk"]
    assert {a.details["reason"] for a in disk} == {"smart", "free", "guard"} and len(disk) == 3
    assert {a.severity for a in disk if a.details["reason"] in ("smart", "free")} == {"critical"}
    # hueco de grabación abierto con la cámara con vídeo → «recording_gap»; al volver a grabar, «…_cleared»
    from vms.core.interfaces import RecordingSpan
    from vms.ops.report import CameraInput
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=1)
    gap_inputs = [CameraInput(camera_id=cam, name="Cajas 2", record=True, online_now=True,
                              spans=[RecordingSpan(start=start, duration=2880)],
                              score_min=None, causes=[], clock=None, days_on_disk=None, protected=0)]
    await ops._recording_gap_alerts(start, now, gap_inputs)
    await ops._recording_gap_alerts(start, now, gap_inputs)        # no se repite
    ok_inputs = [CameraInput(camera_id=cam, name="Cajas 2", record=True, online_now=True,
                             spans=[RecordingSpan(start=start, duration=3600)], score_min=None,
                             causes=[], clock=None, days_on_disk=None, protected=0)]
    await ops._recording_gap_alerts(start, now, ok_inputs)
    kinds = [a.kind for a in emitted if a.kind.startswith("recording_gap")]
    assert kinds == ["recording_gap", "recording_gap_cleared"]


# =========================================================================== exportaciones: caducidad y restos
async def test_interrupted_exports_are_failed_and_leftovers_removed_on_start(api: Harness) -> None:
    ops = api.app.state.ops
    req = EvidenceExportRequest(camera_ids=["cam-00000001"], start=T0, end=T0 + timedelta(minutes=1),
                                reason="Prueba de arranque")
    stuck = EvidenceExport(export_id="ev-20261004-aaaaaa", created_by="admin", request=req, state="running")
    ops.store.save_export(stuck)
    ops.exports_dir.mkdir(parents=True, exist_ok=True)
    (ops.exports_dir / ".ev-20261004-aaaaaa.tmp").mkdir()
    (ops.exports_dir / ".ev-20261004-aaaaaa.tmp" / "chunk.mp4").write_bytes(b"video copiado")
    (ops.exports_dir / "ev-20261004-aaaaaa.zip.part").write_bytes(b"PK")
    assert ops.recover_exports() == ["ev-20261004-aaaaaa"]
    after = ops.store.get_export("ev-20261004-aaaaaa")
    assert after is not None and after.state == "failed" and "reinici" in after.error
    assert list(ops.exports_dir.iterdir()) == []


async def test_exports_expire_and_are_listed_paginated(api: Harness, caplog: pytest.LogCaptureFixture) -> None:
    admin = await api.login()
    ops = api.app.state.ops
    ops.exports_dir.mkdir(parents=True, exist_ok=True)
    req = EvidenceExportRequest(camera_ids=["cam-00000001"], start=T0, end=T0 + timedelta(minutes=1),
                                reason="Prueba de caducidad")
    now = datetime.now(timezone.utc)
    for i in range(60):
        e = EvidenceExport(export_id=f"ev-20261004-{i:06x}", created_by="admin", request=req, state="done",
                           created_at=now - timedelta(days=i))
        ops.store.save_export(e)
        ops.builder.zip_path(e.export_id).write_bytes(b"PK zip de prueba")
    # paginado: las 60 se ven (antes solo las 50 últimas)
    page1 = (await admin.get("/api/evidence/exports", params={"limit": 50})).json()
    page2 = (await admin.get("/api/evidence/exports", params={"limit": 50, "offset": 50})).json()
    assert len(page1) == 50 and len(page2) == 10 and page1[0]["export_id"] == "ev-20261004-000000"
    # caducidad configurable (30 por defecto)
    assert (await admin.get("/api/evidence/settings")).json() == {"export_retention_days": 30}
    assert (await admin.put("/api/evidence/settings", json={"export_retention_days": 0})).status_code == 422
    with caplog.at_level(logging.INFO, logger="vms.audit"):
        removed = await asyncio.to_thread(ops.expire_exports)
    assert len(removed) == 30 and all(int(x[-6:], 16) >= 30 for x in removed), removed
    assert not ops.builder.zip_path("ev-20261004-00001e").exists()          # 30 días
    assert ops.builder.zip_path("ev-20261004-00001d").exists()              # 29 días
    assert '"evidence_export_expired"' in caplog.text
    r = await admin.put("/api/evidence/settings", json={"export_retention_days": 7})
    assert r.status_code == 200
    await asyncio.to_thread(ops.expire_exports)
    left = (await admin.get("/api/evidence/exports", params={"limit": 200})).json()
    assert {e["export_id"] for e in left} == {f"ev-20261004-{i:06x}" for i in range(7)}


async def test_export_checks_free_space_and_leaves_no_staging(tmp_path: Path) -> None:
    from vms.core.paths import AppPaths
    from vms.ops.evidence.export import CameraInfo, ExportContext
    from vms.ops.evidence.keys import load_or_create
    rec = tmp_path / "rec"
    write_segments(rec, "cam-00000001", T0, 3, size=40_000)
    req = EvidenceExportRequest(camera_ids=["cam-00000001"], start=T0, end=T0 + timedelta(minutes=2),
                                reason="Prueba de espacio", include_mp4=False)
    exp = EvidenceExport(export_id="ev-20261004-bbbbbb", created_by="ana", request=req)
    ctx = ExportContext(product_version="2.0.0", site={"id": "s1", "name": "Tienda", "code": "1", "timezone": "UTC"},
                        cameras={"cam-00000001": CameraInfo("cam-00000001", "Caja")}, recordings_dir=rec,
                        protected_dir=tmp_path / "prot", segment_seconds=60,
                        key=load_or_create(AppPaths(tmp_path / "datos").ensure()))
    full = EvidenceBuilder(tmp_path / "exports", disk_free=lambda p: 10_000)
    with pytest.raises(NotEnoughSpace, match="espacio libre"):
        await full.build(exp, ctx)
    roomy = EvidenceBuilder(tmp_path / "exports2")
    path, _, _ = await roomy.build(exp, ctx)
    assert sorted(p.name for p in (tmp_path / "exports2").iterdir()) == [path.name], "sin restos de staging"


# =========================================================================== marcadores
async def test_operator_protection_limits(api: Harness) -> None:
    """Hallazgo: un operador sin ámbito podía proteger tramos de cualquier cámara hasta 10 años, sin límite."""
    admin = await api.login()
    cam = (await new_device(admin, import_channels=[1]))["cameras"][0]
    rec = Path(api.state.recordings_dir())
    write_segments(rec, cam, T0, 30, seconds=3600, size=2048)      # 30 h de grabación (segmentos de 1 h)
    op = await api.operator()

    def body(h0: int, hours: int, days: int, **over: Any) -> dict[str, Any]:
        return {"camera_id": cam, "start": (T0 + timedelta(hours=h0)).isoformat(),
                "end": (T0 + timedelta(hours=h0 + hours)).isoformat(), "protect": True,
                "protect_reason": "Denuncia", "protect_days": days, **over}
    r = await op.post("/api/bookmarks", json=body(0, 1, 365))
    assert r.status_code == 422 and "90" in r.json()["error"]["message"]
    assert (await op.post("/api/bookmarks", json=body(0, 20, 90))).status_code == 201
    assert (await op.post("/api/bookmarks", json=body(0, 20, 90))).status_code == 201       # 40 h
    r = await op.post("/api/bookmarks", json=body(0, 10, 90))                                # 50 h > 48 h
    assert r.status_code == 422 and "48" in r.json()["error"]["message"]
    # un administrador sí, pero más de 90 días exige número de caso
    r = await admin.post("/api/bookmarks", json=body(0, 1, 365))
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["loc"] == ["case_ref"]
    assert (await admin.post("/api/bookmarks", json=body(0, 1, 365, case_ref="AT-9"))).status_code == 201


def test_release_failure_is_retried_by_the_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hallazgo: `rmtree(ignore_errors=True)` dejaba copias protegidas huérfanas para siempre en Windows."""
    store = OpsStore(tmp_path / "ops.sqlite3")
    rec = tmp_path / "rec"
    write_segments(rec, "cam-00000001", T0, 2)
    mgr = BookmarkManager(store, tmp_path / "prot", lambda: str(rec), lambda: 60)
    from vms.ops.models import BookmarkCreate
    bm = mgr.create(BookmarkCreate(camera_id="cam-00000001", start=T0, end=T0 + timedelta(seconds=90), protect=True,
                                   protect_reason="Denuncia"), "admin")
    folder = tmp_path / "prot" / "cam-00000001" / bm.id
    assert folder.is_dir()
    import shutil as _sh
    real = _sh.rmtree

    def locked(path: Any, *a: Any, onexc: Any = None, **k: Any) -> None:     # como un segmento abierto en Windows
        onexc(None, str(next(Path(path).glob("*.mp4"))), PermissionError("en uso"))
    monkeypatch.setattr("vms.ops.evidence.bookmarks.shutil.rmtree", locked)
    released = mgr.release(bm, "a mano")
    assert not released.protected and folder.is_dir()           # liberado, la copia sigue (no se pudo borrar)
    assert mgr.sweep_orphans() == []                             # sigue bloqueado: no se da por borrado
    monkeypatch.setattr("vms.ops.evidence.bookmarks.shutil.rmtree", real)
    assert mgr.sweep_orphans() == [f"cam-00000001/{bm.id}"] and not folder.exists()
    # una carpeta de un marcador que ya no existe también se barre
    (tmp_path / "prot" / "cam-00000002" / "bm-deadbeef").mkdir(parents=True)
    assert mgr.sweep_orphans() == ["cam-00000002/bm-deadbeef"]
    assert isinstance(store.get_bookmark(bm.id), Bookmark)
    store.close()


# =========================================================================== descargas: permiso vigente
async def test_download_revalidates_current_permissions(api: Harness) -> None:
    """Hallazgo: un operador al que se le quita el permiso `export` seguía descargando sus ZIP anteriores."""
    admin = await api.login()
    cam = (await new_device(admin, import_channels=[1]))["cameras"][0]
    write_segments(Path(api.state.recordings_dir()), cam, T0, 3)
    op = await api.operator()
    r = await op.post("/api/evidence/exports", json={"camera_ids": [cam], "start": T0.isoformat(),
                                                     "end": (T0 + timedelta(minutes=2)).isoformat(),
                                                     "reason": "Hurto", "include_mp4": False})
    assert r.status_code == 202, r.text
    eid = r.json()["export_id"]

    async def done() -> Any:
        e = (await op.get(f"/api/evidence/exports/{eid}")).json()
        return e if e["state"] == "done" else None
    exp = await wait_for(done)
    assert (await op.get(exp["download_url"])).status_code == 200
    r = await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [cam], "export": False}})
    assert r.status_code == 200
    assert (await op.get(exp["download_url"])).status_code == 404
    assert (await op.get(f"/api/evidence/exports/{eid}")).status_code == 404
    assert (await op.get("/api/evidence/exports")).json() == []
    assert (await admin.get(exp["download_url"])).status_code == 200


# =========================================================================== instantáneas: contraseña rechazada
class _AuthFailClient:
    calls = 0

    def __init__(self, *_: Any) -> None:
        pass

    async def snapshot(self, channel: int, stream: str) -> bytes:
        type(self).calls += 1
        raise DeviceAuthFailed("Usuario o contraseña incorrectos")

    async def aclose(self) -> None:
        return None


async def test_health_snapshot_stops_using_a_rejected_password(api: Harness) -> None:
    """Hallazgo: con la contraseña mala, la salud la probaba cada 3 min por cámara (bloqueo de cuenta)."""
    admin = await api.login()
    cam_id = (await new_device(admin, import_channels=[1]))["cameras"][0]
    cam = api.state.config().camera(cam_id)
    assert cam is not None
    _AuthFailClient.calls = 0
    real_factory = api.state.client_factory
    api.state.client_factory = _AuthFailClient  # type: ignore[assignment]
    t = [0.0]
    backoff = AuthBackoff(seconds=1800, clock=lambda: t[0])
    try:
        for _ in range(5):
            await grab(api.state, cam, backoff)
        assert _AuthFailClient.calls == 1, "un solo intento con credenciales hasta que cambie algo"
        t[0] += 1801                                     # pasa la ventana de bloqueo → un intento más
        await grab(api.state, cam, backoff)
        assert _AuthFailClient.calls == 2
        api.state.creds.set_device_password(cam.device_id, "Otra-clave-1")   # contraseña cambiada → se reintenta
        await grab(api.state, cam, backoff)
        assert _AuthFailClient.calls == 3
    finally:
        api.state.client_factory = real_factory


# =========================================================================== hora del PC y clave de evidencias
def test_pc_clock_message_is_about_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clockmod, "windows_time_config", lambda: ("ntp", "time.windows.com"))
    monkeypatch.setattr(clockmod, "sntp_offset", lambda server: (45.0, 20.0))
    chk = clockmod.pc_check(2.0, 30.0)
    assert chk.status == "critical" and "hora del PC" in chk.message_es
    assert "sincronización de hora de Windows" in chk.message_es and "Activa NTP en el equipo" not in chk.message_es
    calls: list[str] = []
    monkeypatch.setattr(clockmod, "sntp_offset", lambda server: calls.append(server) or (0.0, 1.0))
    off = clockmod.pc_check(2.0, 30.0, sntp=False)
    assert calls == [] and off.status == "unknown" and "desactivada" in off.message_es


async def test_clock_settings_route(api: Harness) -> None:
    admin = await api.login()
    assert (await admin.get("/api/clock/settings")).json() == {"pc_sntp_enabled": True}
    assert (await admin.put("/api/clock/settings", json={"pc_sntp_enabled": False})).status_code == 200
    assert api.app.state.ops.pc_sntp_enabled() is False
    op = await api.operator()
    assert (await op.put("/api/clock/settings", json={"pc_sntp_enabled": True})).status_code == 403


async def test_evidence_key_exists_from_the_start(api: Harness) -> None:
    """Hallazgo: la clave se creaba en la primera exportación; la central no conocía el key_id hasta entonces."""
    from vms.ops.heartbeat import payload_evidence_key
    await api.login()
    key = payload_evidence_key(api.state.paths)
    assert key is not None and len(key["key_id"]) == 64


# =========================================================================== CSV
def test_csv_cells_cannot_inject_formulas() -> None:
    for bad in ("=HYPERLINK(\"http://x\")", "+1", "-2+3", "@SUM(A1)", "\tx", "\rx"):
        assert text_cell(bad).startswith("'")
    assert text_cell("Cajas 2") == "Cajas 2" and text_cell(None) == ""
    rep = HealthReport(site_id="=cmd", date="2026-10-04", generated_at=T0, status="ok", problems=[],
                       cameras=[CameraDayReport(camera_id="cam-00000001", name="=1+1", online_ratio=1.0,
                                                recording_gaps_min=0, clock_skew_s=-3.2)])
    rows = list(csv.reader(io.StringIO(report_csv(rep).decode("utf-8-sig")), delimiter=";"))
    assert rows[1][0] == "'=cmd" and rows[1][2] == "'=1+1" and rows[1][7] == "-3,2"   # el número negativo, tal cual
    from central.ops import problems_csv
    out = problems_csv([{"name": "@Tienda", "code": "-1", "status": "critical", "report_date": None,
                         "score_min": None, "cameras_critical": 1, "cameras_warning": 0, "clock_worst_s": -2.5,
                         "forecast_days": None, "problems": ["=evil()"]}]).decode("utf-8-sig")
    row = list(csv.reader(io.StringIO(out), delimiter=";"))[1]
    assert row[0] == "'@Tienda" and row[1] == "'-1" and row[7] == "-2,5" and row[9] == "'=evil()"


def test_central_exposes_the_evidence_key_id() -> None:
    from central.ops import site_problem
    row = {"site_id": "s1", "name": "T", "code": "1", "timezone": "UTC", "last_seen": T0, "reported_status": "ok",
           "health": None, "evidence_key": {"key_id": "ab" * 32, "public_key": "x"}}
    assert site_problem(row, T0, None)["evidence_key_id"] == "ab" * 32

