"""`python -m vms` como proceso real: arranca, responde, para sin dejar MediaMTX huérfano y se
recupera si el backend muere de golpe (el MediaMTX que quedó se detiene en el siguiente arranque)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import psutil
import pytest

from tests.conftest import ROOT, get_free_port

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx]


def _env(data: Path, mediamtx_bin: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VMS_")}
    env.update({
        "VMS_ENV_FILE": str(data / "no-existe.env"), "VMS_DATA_DIR": str(data), "VMS_MEDIAMTX_BIN": mediamtx_bin,
        "VMS_HTTP_HOST": "127.0.0.1", "VMS_HTTP_PORT": str(get_free_port()),
        "VMS_ADMIN_INITIAL_PASSWORD": "Admin#12345", "VMS_CREDENTIAL_BACKEND": "file",
        "VMS_MTX_RTSP_ADDRESS": f"127.0.0.1:{get_free_port()}", "VMS_MTX_WEBRTC_ADDRESS": f"127.0.0.1:{get_free_port()}",
        "VMS_MTX_WEBRTC_ICE_UDP": f":{get_free_port()}", "VMS_MTX_WEBRTC_ICE_TCP": "",
        "VMS_MTX_API_ADDRESS": f"127.0.0.1:{get_free_port()}", "VMS_MTX_PLAYBACK_ADDRESS": f"127.0.0.1:{get_free_port()}",
        "VMS_MTX_METRICS_ADDRESS": f"127.0.0.1:{get_free_port()}",
    })
    return env


def _start(env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    with open(log, "ab") as fh:
        return subprocess.Popen([sys.executable, "-m", "vms"], cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)


def _wait_health(port: str, proc: subprocess.Popen[bytes], log: Path, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert proc.poll() is None, f"el backend terminó: {log.read_text(errors='replace')[-2000:]}"
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)
            if r.status_code == 200 and r.json()["engine"]["running"]:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise AssertionError(f"el backend no respondió: {log.read_text(errors='replace')[-2000:]}")


def _stop(proc: subprocess.Popen[bytes], log: Path) -> None:
    """Parada ordenada. uvicorn vuelve a lanzar la señal tras cerrar, así que -15 también es correcto."""
    before = log.read_text(encoding="utf-8", errors="replace").count("MediaMTX detenido")
    proc.send_signal(signal.SIGTERM if sys.platform != "win32" else signal.CTRL_BREAK_EVENT)
    assert proc.wait(20) in (0, -signal.SIGTERM)
    text = log.read_text(encoding="utf-8", errors="replace")
    assert text.count("MediaMTX detenido") == before + 1, text[-1500:]


def _mtx_pid(data: Path) -> int:
    return int((data / "mediamtx" / "mediamtx.pid").read_text())


@pytest.fixture
def procs() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for p in started:
        if p.poll() is None:
            p.kill()
            p.wait(10)


def test_backend_process_lifecycle(tmp_path: Path, mediamtx_bin: str, procs: list[subprocess.Popen[bytes]]) -> None:
    data, log = tmp_path / "data", tmp_path / "backend.log"
    env = _env(data, mediamtx_bin)
    port = env["VMS_HTTP_PORT"]

    proc = _start(env, log)
    procs.append(proc)
    health = _wait_health(port, proc, log)
    assert health["status"] == "ok"
    pid = _mtx_pid(data)
    assert psutil.pid_exists(pid)
    assert (data / "logs" / "vms.log").is_file() and (data / "config" / "users.json").is_file()

    # Parada ordenada (como el servicio de Windows o systemd): MediaMTX se para con el backend
    _stop(proc, log)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE

    # Muerte súbita del backend: el MediaMTX que quede se detiene en el siguiente arranque
    proc2 = _start(env, log)
    procs.append(proc2)
    _wait_health(port, proc2, log)
    orphan = _mtx_pid(data)
    proc2.kill()
    proc2.wait(10)
    time.sleep(0.5)
    proc3 = _start(env, log)
    procs.append(proc3)
    _wait_health(port, proc3, log)
    assert not psutil.pid_exists(orphan) or psutil.Process(orphan).status() == psutil.STATUS_ZOMBIE
    assert _mtx_pid(data) != orphan
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "Admin#12345" not in text
    _stop(proc3, log)
