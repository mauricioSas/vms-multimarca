"""Piezas del motor sin procesos externos: mediamtx.yml, rutas, vigilante de disco, salida de MediaMTX."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from vms.core.interfaces import CameraSource
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine, explain_source_error
from vms.engine import disk_guard
from vms.engine.mtx_api import format_time, parse_time
from vms.engine.mtx_config import conf_hash, global_config, path_configs, split_address, write_config

SRC = CameraSource(camera_id="cam-12345678", name="Puerta",
                   main_url="rtsp://admin:S%23cr%40t@10.0.0.5:554/Streaming/Channels/101",
                   sub_url="rtsp://admin:S%23cr%40t@10.0.0.5:554/Streaming/Channels/102", record=True)


# --------------------------------------------------------------------------- configuración
def test_global_config_has_no_credentials_and_follows_contract(settings: VmsSettings, tmp_path: Path) -> None:
    cfg = global_config(settings, RecordingSettings(segment_seconds=600, part_seconds=1),
                        RetentionSettings(days=7), tmp_path / "rec")
    assert cfg["apiAddress"] == settings.mtx_api_address and cfg["api"] is True
    assert cfg["rtspAddress"] == settings.mtx_rtsp_address and cfg["rtspTransports"] == ["tcp"]
    assert cfg["webrtcLocalTCPAddress"] == ""  # vacío = desactivado (fixture)
    for proto in ("rtmp", "hls", "srt", "moq", "pprof"):
        assert cfg[proto] is False
    perms = {p["action"] for p in cfg["authInternalUsers"][0]["permissions"]}
    assert perms == {"read", "playback", "api", "metrics"} and "publish" not in perms
    assert cfg["authInternalUsers"][0]["ips"] == ["127.0.0.1", "::1"]
    pd = cfg["pathDefaults"]
    assert pd["recordFormat"] == "fmp4" and pd["recordPartDuration"] == "1s"
    assert pd["recordSegmentDuration"] == "600s" and pd["recordDeleteAfter"] == "168h"
    assert pd["recordPath"].endswith("/rec/%path/%Y-%m-%d_%H-%M-%S-%f%z") and "\\" not in pd["recordPath"]
    assert cfg["paths"] == {}
    f = tmp_path / "mediamtx.yml"
    write_config(f, cfg)
    assert yaml.safe_load(f.read_text(encoding="utf-8"))["pathDefaults"]["recordDeleteAfter"] == "168h"


def test_disabled_services_when_address_empty(settings: VmsSettings, tmp_path: Path) -> None:
    s = settings.model_copy(update={"mtx_webrtc_address": "", "mtx_playback_address": "", "mtx_metrics_address": ""})
    cfg = global_config(s, RecordingSettings(), RetentionSettings(), tmp_path)
    assert cfg["webrtc"] is False and cfg["playback"] is False and cfg["metrics"] is False
    assert "webrtcAddress" not in cfg and "playbackAddress" not in cfg


def test_path_configs_main_records_sub_on_demand() -> None:
    confs = path_configs(SRC)
    assert set(confs) == {"cam-12345678/main", "cam-12345678/sub"}
    main, sub = confs["cam-12345678/main"], confs["cam-12345678/sub"]
    assert main["record"] is True and main["sourceOnDemand"] is False and main["rtspTransport"] == "tcp"
    assert sub["record"] is False and sub["sourceOnDemand"] is True and sub["sourceOnDemandCloseAfter"] == "30s"
    no_sub = path_configs(SRC.model_copy(update={"sub_url": None, "record": False}))
    assert list(no_sub) == ["cam-12345678/main"] and no_sub["cam-12345678/main"]["record"] is False
    assert conf_hash(main) == conf_hash(dict(main)) != conf_hash(sub)


def test_camera_source_repr_hides_urls() -> None:
    assert "S%23cr" not in repr(SRC) and "10.0.0.5" not in str(SRC)


def test_split_address() -> None:
    assert split_address("127.0.0.1:8554") == ("127.0.0.1", 8554)
    assert split_address(":8189") == ("127.0.0.1", 8189)
    assert split_address("0.0.0.0:9") == ("127.0.0.1", 9)
    assert split_address("") is None


def test_time_helpers_normalize_to_utc() -> None:
    dt = parse_time("2026-10-04T16:00:09.926811123+02:00")
    assert dt == datetime(2026, 10, 4, 14, 0, 9, 926811, tzinfo=timezone.utc)
    assert format_time(dt) == "2026-10-04T14:00:09.926811Z"
    assert parse_time("2026-10-04T14:00:00Z").tzinfo is not None


# --------------------------------------------------------------------------- vigilante de disco
def _make_segment(root: Path, cam: str, stream: str, stamp: str, size: int, age_s: float) -> Path:
    d = root / cam / stream
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{stamp}-000000.mp4"
    f.write_bytes(b"\0" * size)
    t = time.time() - age_s
    os.utime(f, (t, t))
    return f


def test_disk_guard_deletes_oldest_but_protects_newest_and_recent(tmp_path: Path) -> None:
    root = tmp_path / "rec"
    a1 = _make_segment(root, "cam-aaaa0001", "main", "2026-10-01_10-00-00", 100, 9000)
    b1 = _make_segment(root, "cam-bbbb0001", "main", "2026-10-01_11-00-00", 100, 8000)
    a2 = _make_segment(root, "cam-aaaa0001", "main", "2026-10-02_10-00-00", 100, 7000)
    a3 = _make_segment(root, "cam-aaaa0001", "main", "2026-10-03_10-00-00", 100, 10)   # en escritura
    b2 = _make_segment(root, "cam-bbbb0001", "main", "2026-10-02_11-00-00", 100, 6000)  # más reciente de b
    other = root / "cam-aaaa0001" / "main" / "notas.txt"
    other.write_text("no tocar")
    (root / "no-es-camara!").mkdir()
    state = {"used": 900}

    def usage(_: Path) -> tuple[int, int, int]:
        return 1000, state["used"], 1000 - state["used"]

    def fake_unlink_usage(p: Path) -> tuple[int, int, int]:
        remaining = sum(f.stat().st_size for f in root.rglob("*.mp4"))
        state["used"] = 400 + remaining
        return usage(p)

    res = disk_guard.run(root, 80, usage_fn=fake_unlink_usage)
    # 400 + 500 = 900 > 800 → hay que liberar 100: se borra solo el más antiguo de todos
    assert res.deleted == [a1] and res.target_reached
    assert not a1.exists() and b1.exists() and a2.exists() and a3.exists() and b2.exists() and other.exists()


def test_disk_guard_does_not_wipe_history_when_it_would_not_help(tmp_path: Path) -> None:
    root = tmp_path / "rec"
    old = _make_segment(root, "cam-aaaa0001", "main", "2026-10-01_10-00-00", 10, 9000)
    _make_segment(root, "cam-aaaa0001", "main", "2026-10-02_10-00-00", 10, 9000)
    res = disk_guard.run(root, 50, usage_fn=lambda p: (1000, 950, 50))
    assert res.deleted == [] and not res.target_reached and old.exists()


def test_disk_guard_disabled_or_below_threshold(tmp_path: Path) -> None:
    root = tmp_path / "rec"
    f = _make_segment(root, "cam-aaaa0001", "main", "2026-10-01_10-00-00", 10, 9000)
    _make_segment(root, "cam-aaaa0001", "main", "2026-10-02_10-00-00", 10, 9000)
    assert disk_guard.run(root, 0, usage_fn=lambda p: (100, 99, 1)).deleted == []
    assert disk_guard.run(root, 90, usage_fn=lambda p: (100, 50, 50)).deleted == []
    assert f.exists()


def test_segment_name_parsing() -> None:
    # Formato actual (con desfase horario): inicio en UTC
    assert disk_guard.parse_segment_name("2026-10-04_16-00-36-313376+0200.mp4") == datetime(
        2026, 10, 4, 14, 0, 36, 313376, tzinfo=timezone.utc)
    # Formato antiguo (sin desfase): se interpreta en la hora local del equipo
    legacy = disk_guard.parse_segment_name("2026-10-04_16-00-36-313376.mp4")
    assert legacy == datetime(2026, 10, 4, 16, 0, 36, 313376).astimezone().astimezone(timezone.utc)
    assert disk_guard.parse_segment_name("2026-10-04_16-00-36.mkv") is None


# --------------------------------------------------------------------------- salida de MediaMTX
def test_explain_source_error() -> None:
    assert explain_source_error("bad status code: 401 (Unauthorized)").startswith("Usuario o contraseña")
    assert explain_source_error("dial tcp 10.0.0.5:554: connect: connection refused").startswith("El equipo rechaza")
    assert explain_source_error("dial tcp: i/o timeout").startswith("El equipo no responde")
    assert explain_source_error("bad status code: 404 (Not Found)").startswith("La ruta RTSP no existe")


async def test_log_lines_set_path_errors_and_pause_device_after_401(settings: VmsSettings, app_paths: AppPaths,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    eng = MediaMtxEngine(settings, app_paths, auth_fail_threshold=2)
    eng._sources = {SRC.camera_id: SRC}
    eng._applied = {"cam-12345678/main": "x"}
    line = "2026/10/04 16:00:35 ERR [path cam-12345678/main] [RTSP source] bad status code: 401 (Unauthorized)"
    eng._on_line(line)
    assert "Usuario o contraseña" in eng._path_errors["cam-12345678/main"]
    assert eng.paused_devices == []
    eng._on_line(line)
    assert len(eng.paused_devices) == 1 and eng.paused_devices[0][:3] == ("10.0.0.5", 554, "admin")
    assert "S#cr@t" not in caplog.text and "S%23cr%40t" not in caplog.text
    other = "2026/10/04 16:00:37 ERR [path cam-99999999/main] [RTSP source] EOF"
    eng._on_line(other)
    assert "cortó el vídeo" in eng._path_errors["cam-99999999/main"]
    eng._on_line("2026/10/04 16:00:38 INF [path cam-99999999/main] stream is available and online, 1 track (H264)")
    assert "cam-99999999/main" not in eng._path_errors


def test_auth_guard_new_password_is_not_paused() -> None:
    from vms.engine.engine import _AuthGuard, _cred_key
    g = _AuthGuard(threshold=2, pause_seconds=60)
    bad = SRC.main_url
    good = bad.replace("S%23cr%40t", "Nueva")
    assert not g.failure(bad) and g.failure(bad) and g.is_paused(bad)
    assert not g.is_paused(good)  # otra contraseña = otra clave: se prueba enseguida
    g.forget_unused({_cred_key(good)})  # la contraseña mala ya no la usa ninguna cámara
    assert not g.is_paused(bad)


async def test_engine_start_fails_clearly_without_binary(settings: VmsSettings, app_paths: AppPaths,
                                                         tmp_path: Path) -> None:
    from vms.core.errors import EngineUnavailable
    eng = MediaMtxEngine(settings, app_paths, mediamtx_bin=tmp_path / "no-existe")
    with pytest.raises(EngineUnavailable, match="MediaMTX"):
        await eng.start()
    assert not (await eng.status()).running
