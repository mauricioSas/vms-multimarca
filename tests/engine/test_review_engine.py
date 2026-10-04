"""Regresiones de la revisión del motor de vídeo.

- Un puerto con «401» (p. ej. 5401) no se confunde con una contraseña rechazada.
- Noche del cambio de hora (octubre): nombres de grabación con desfase horario, sin ambigüedad,
  y migración de las grabaciones antiguas.
- MediaMTX sin usuario anónimo: la API y la reproducción exigen el usuario interno.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from vms.core.interfaces import CameraSource
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.mtx_auth import API_USER, READER_USER, MtxCredentials, mtx_hash, with_reader_credentials
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine, explain_source_error
from vms.engine import disk_guard
from vms.engine.engine import is_auth_error
from vms.engine.mtx_config import RECORD_FILE_PATTERN, global_config
from vms.vendors.onvif_client import _translate


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def madrid_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset no existe en Windows")
    monkeypatch.setenv("TZ", "Europe/Madrid")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --------------------------------------------------------------------------- 401 en un puerto
@pytest.mark.parametrize("port", [5401, 4010, 40100, 1401])
def test_port_with_401_is_not_an_auth_error(port: int) -> None:
    raw = f"dial tcp 192.168.1.50:{port}: connect: connection refused"
    assert not is_auth_error(raw)
    assert explain_source_error(raw).startswith("El equipo rechaza la conexión RTSP")
    assert not explain_source_error(f"dial tcp 10.4.0.4:{port}: i/o timeout").startswith("Usuario")


@pytest.mark.parametrize("raw", ["bad status code: 401 (Unauthorized)", "bad status code 401",
                                 "authentication failed: Unauthorized"])
def test_real_401_is_detected(raw: str) -> None:
    assert is_auth_error(raw) and explain_source_error(raw).startswith("Usuario o contraseña")


def test_404_port_is_not_not_found() -> None:
    assert not explain_source_error("dial tcp 10.0.0.9:4040: connect: connection refused").startswith("La ruta")


def test_engine_does_not_pause_device_on_port_5401(settings: VmsSettings, app_paths: AppPaths) -> None:
    eng = MediaMtxEngine(settings, app_paths, auth_fail_threshold=2)
    src = CameraSource(camera_id="cam-door0001", name="Puerta",
                       main_url="rtsp://admin:pw@192.168.1.50:5401/Streaming/Channels/101", sub_url=None)
    eng._sources = {src.camera_id: src}
    eng._applied = {"cam-door0001/main": "x"}
    line = ("2026/10/04 16:00:35 WAR [path cam-door0001/main] [RTSP source] dial tcp 192.168.1.50:5401: "
            "connect: connection refused")
    eng._on_line(line)
    eng._on_line(line)
    assert eng.paused_devices == []
    assert "rechaza la conexión" in eng._path_errors["cam-door0001/main"]


def test_onvif_port_with_401_is_unreachable_not_auth() -> None:
    assert type(_translate(OSError("Cannot connect to host 10.0.0.2:4010 ssl:default"), "x")).__name__ == \
        "DeviceUnreachable"
    assert type(_translate(Exception("HTTP 401 Unauthorized"), "x")).__name__ == "DeviceAuthFailed"


# --------------------------------------------------------------------------- cambio de hora
def test_record_pattern_has_offset() -> None:
    assert RECORD_FILE_PATTERN.endswith("%f%z")


def test_dst_night_names_are_unambiguous() -> None:
    first = disk_guard.parse_segment_name("2026-10-25_02-30-00-000000+0200.mp4")    # CEST
    second = disk_guard.parse_segment_name("2026-10-25_02-30-00-000000+0100.mp4")   # CET, una hora después
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert second == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)


def test_legacy_names_are_migrated_with_the_right_offset(tmp_path: Path, madrid_tz: None) -> None:
    root = tmp_path / "rec"
    d = root / "cam-door0001" / "main"
    d.mkdir(parents=True)

    def legacy(stamp: str, mtime_utc: datetime) -> Path:
        f = d / f"{stamp}-000000.mp4"
        f.write_bytes(b"x")
        ts = mtime_utc.timestamp()
        os.utime(f, (ts, ts))
        return f

    legacy("2026-10-24_12-00-00", datetime(2026, 10, 24, 10, 15, tzinfo=timezone.utc))     # verano
    legacy("2026-10-26_12-00-00", datetime(2026, 10, 26, 11, 15, tzinfo=timezone.utc))     # invierno
    # Hora repetida: el archivo cuya última escritura es 00:59 UTC empezó en la PRIMERA 02:30 (CEST)
    legacy("2026-10-25_02-30-00", datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc))
    (d / "notas.txt").write_text("no tocar")
    assert disk_guard.migrate_legacy_names(root) == 3
    names = sorted(p.name for p in d.iterdir())
    assert names == ["2026-10-24_12-00-00-000000+0200.mp4", "2026-10-25_02-30-00-000000+0200.mp4",
                     "2026-10-26_12-00-00-000000+0100.mp4", "notas.txt"]
    assert disk_guard.migrate_legacy_names(root) == 0   # idempotente


def test_legacy_ambiguous_second_hour(madrid_tz: None) -> None:
    local = datetime(2026, 10, 25, 2, 30)
    assert disk_guard.legacy_offset(local, datetime(2026, 10, 25, 1, 59, tzinfo=timezone.utc).timestamp()) == "+0100"
    assert disk_guard.legacy_offset(local, datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc).timestamp()) == "+0200"


@pytest.mark.needs_mediamtx
@pytest.mark.needs_ffmpeg
def test_mediamtx_lists_both_dst_hours_separately(tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str) -> None:
    """MediaMTX real: graba un segmento con el patrón del producto y lo copia con nombres de la noche
    del cambio de hora; /list debe devolver dos tramos distintos para las dos «02:30»."""
    rtsp, pb = _port(), _port()
    rec = tmp_path / "rec"
    conf = {
        "logLevel": "warn", "api": False, "rtsp": True, "rtspAddress": f"127.0.0.1:{rtsp}", "rtmp": False,
        "hls": False, "webrtc": False, "srt": False, "moq": False, "metrics": False, "pprof": False,
        "playback": True, "playbackAddress": f"127.0.0.1:{pb}",
        "authInternalUsers": [{"user": "any", "pass": "", "ips": [],
                               "permissions": [{"action": "publish"}, {"action": "playback"}]}],
        "pathDefaults": {"recordPath": f"{rec.as_posix()}/{RECORD_FILE_PATTERN}", "recordFormat": "fmp4",
                         "recordPartDuration": "1s", "recordSegmentDuration": "1h", "recordDeleteAfter": "0s"},
        "paths": {"cam": {"record": True}},
    }
    yml = tmp_path / "m.yml"
    yml.write_text(yaml.safe_dump(conf), encoding="utf-8")
    env = {**os.environ, "TZ": "Europe/Madrid"}

    def start() -> subprocess.Popen[bytes]:
        p = subprocess.Popen([mediamtx_bin, str(yml)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", pb)) == 0:
                    return p
            time.sleep(0.1)
        p.kill()
        raise AssertionError("MediaMTX no arrancó")

    mtx = start()
    try:
        subprocess.run([ffmpeg_bin, "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                        "-t", "3", "-c:v", "libx264", "-g", "10", "-f", "rtsp", "-rtsp_transport", "tcp",
                        f"rtsp://127.0.0.1:{rtsp}/cam"], check=True, timeout=30)
        time.sleep(1)
    finally:
        mtx.terminate()
        mtx.wait(10)
    recorded = sorted((rec / "cam").glob("*.mp4"))
    assert recorded and recorded[0].name[-9:-4] in ("+0200", "+0100")   # MediaMTX escribe el desfase
    seg = recorded[0]
    for stamp in ("2026-10-25_02-30-00-000000+0200", "2026-10-25_02-30-00-000000+0100"):
        shutil.copy(seg, rec / "cam" / f"{stamp}.mp4")
    for f in recorded:
        f.unlink()
    mtx = start()
    try:
        items = httpx.get(f"http://127.0.0.1:{pb}/list", params={"path": "cam"}, timeout=5).json()
    finally:
        mtx.terminate()
        mtx.wait(10)
    starts = sorted(datetime.fromisoformat(i["start"]).astimezone(timezone.utc) for i in items)
    assert starts == [datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
                      datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)]


# --------------------------------------------------------------------------- MediaMTX sin anónimos
def test_mediamtx_config_has_no_anonymous_user(settings: VmsSettings, tmp_path: Path) -> None:
    creds = MtxCredentials.from_internal_token("token-x")
    cfg = global_config(settings, RecordingSettings(), RetentionSettings(), tmp_path, creds=creds)
    users = cfg["authInternalUsers"]
    assert all(u["user"] != "any" for u in users) and len(users) == 2
    text = yaml.safe_dump(cfg)
    assert creds.api_password not in text and creds.reader_password not in text   # solo hashes
    api_user = next(u for u in users if u["user"] == mtx_hash(API_USER))
    reader = next(u for u in users if u["user"] == mtx_hash(READER_USER))
    assert {p["action"] for p in api_user["permissions"]} == {"api", "playback", "read", "metrics"}
    assert [p["action"] for p in reader["permissions"]] == ["read"]
    assert all("publish" not in str(u["permissions"]) for u in users)
    assert all(u["ips"] == ["127.0.0.1", "::1"] for u in users)


def test_reader_credentials_only_for_local_mediamtx() -> None:
    creds = MtxCredentials.from_internal_token("token-x")
    local = with_reader_credentials("rtsp://127.0.0.1:8554/cam-x/sub", creds, "127.0.0.1:8554")
    assert local.startswith(f"rtsp://{READER_USER}:{creds.reader_password}@127.0.0.1:8554/")
    assert with_reader_credentials("rtsp://127.0.0.1:9554/x", creds, "127.0.0.1:8554") == "rtsp://127.0.0.1:9554/x"
    assert with_reader_credentials("rtsp://10.0.0.5:554/x", creds) == "rtsp://10.0.0.5:554/x"
    assert with_reader_credentials("rtsp://u:p@127.0.0.1:8554/x", creds) == "rtsp://u:p@127.0.0.1:8554/x"
    assert MtxCredentials.from_internal_token("a") != MtxCredentials.from_internal_token("b")


@pytest.mark.needs_mediamtx
async def test_mediamtx_api_rejects_anonymous_and_runoninit(settings: VmsSettings, app_paths: AppPaths,
                                                            mediamtx_bin: str, tmp_path: Path) -> None:
    """Prueba del hallazgo crítico con MediaMTX real: sin credenciales no se leen las URL de origen
    (con contraseñas) ni se puede añadir una ruta con `runOnInit` (ejecución de comandos)."""
    eng = MediaMtxEngine(settings, app_paths, mediamtx_bin=Path(mediamtx_bin))
    await eng.start()
    marker = tmp_path / "pwned"
    try:
        await eng.apply([CameraSource(camera_id="cam-sec00001", name="x",
                                      main_url="rtsp://admin:S3cr3t%40Pass@192.0.2.10:554/a", sub_url=None,
                                      record=False)],
                        RecordingSettings(), RetentionSettings(), eng.recordings_dir)
        base = f"http://{settings.mtx_api_address}"
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{base}/v3/config/paths/list")
            assert r.status_code == 401 and "S3cr3t" not in r.text
            r = await c.post(f"{base}/v3/config/paths/add/evil", content=f'{{"runOnInit": "touch {marker}"}}',
                             headers={"Content-Type": "text/plain", "Origin": "http://evil.example"})
            assert r.status_code == 401
            r = await c.get(f"{base}/v3/config/paths/list", auth=(READER_USER, eng.mtx_credentials.reader_password))
            assert r.status_code == 401   # el usuario de lectura no puede usar la API
            r = await c.get(f"{base}/v3/config/paths/list", auth=eng.http_credentials())
            assert r.status_code == 200
            r = await c.get(f"http://{settings.mtx_playback_address}/list", params={"path": "cam-sec00001/main"})
            assert r.status_code == 401
        assert not marker.exists()
        st = await eng.status()
        assert st.running
    finally:
        await eng.stop()
