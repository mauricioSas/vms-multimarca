"""Parada ordenada de `VMSHeartbeat` (`python -m central.agent`) y `VMSCentral` (`python -m central`) bajo
`vmsctl run` (CONTRATO §14.1).

En Windows, la única orden de parada que manda vmsctl es cerrar la entrada estándar (`VMS_STOP_ON_STDIN_EOF=1`):
ni SIGTERM ni Ctrl+Break. Los dos solo atendían señales, así que vmsctl los mataba a los 10 s en cada parada,
igual que al actualizador en el PC de la tienda («no paró en 10 s: se fuerza»).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import get_free_port

ROOT = Path(__file__).resolve().parents[2]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VMS_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", VMS_STOP_ON_STDIN_EOF="1",
               VMS_DATA_DIR=str(tmp_path / "data"),
               # Sin el .env del repositorio ni el del producto instalado (CONTRATO §3.1), como el resto de pruebas
               VMS_ENV_FILE=str(tmp_path / "no-existe.env"), VMS_ENV_FILE_ONLY="1", **extra)
    return env


def _stops_when_stdin_closes(args: list[str], env: dict[str, str], ready: Callable[[str], bool],
                             tmp_path: Path) -> str:
    err = tmp_path / "stderr.txt"
    with open(err, "wb") as ferr:
        proc = subprocess.Popen([sys.executable, *args], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=ferr, env=env, cwd=ROOT)
        try:
            deadline = time.monotonic() + 60
            while not ready(err.read_text(encoding="utf-8", errors="replace")):
                assert proc.poll() is None, err.read_text(encoding="utf-8", errors="replace")[-2000:]
                assert time.monotonic() < deadline, "no llegó a arrancar"
                time.sleep(0.1)
            assert proc.stdin is not None
            proc.stdin.close()                       # lo que hace vmsctl run al parar el servicio
            try:
                code = proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pytest.fail("no paró en 8 s tras cerrar su entrada estándar (vmsctl lo mata a los 10)")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    log = err.read_text(encoding="utf-8", errors="replace")
    assert code == 0, log[-2000:]
    assert "Parada pedida por el servicio" in log
    return log


def test_heartbeat_agent_stops_when_vmsctl_closes_stdin(tmp_path: Path) -> None:
    dead = f"http://127.0.0.1:{get_free_port()}"          # ni panel central ni backend: nada de red real
    env = _env(tmp_path, VMS_CENTRAL_URL=dead, VMS_SITE_TOKEN="token-de-prueba", VMS_AGENT_BACKEND_URL=dead,
               VMS_SITE_ID="site-prueba")
    log = _stops_when_stdin_closes(["-m", "central.agent"], env, lambda text: "Agente de latido de" in text,
                                   tmp_path)
    assert "token-de-prueba" not in log


def test_central_panel_stops_when_vmsctl_closes_stdin(tmp_path: Path) -> None:
    port = get_free_port()
    env = _env(tmp_path, VMS_CENTRAL_HTTP_HOST="127.0.0.1", VMS_CENTRAL_HTTP_PORT=str(port),
               VMS_CENTRAL_DATA_DIR=str(tmp_path / "central"))

    def listening(_text: str) -> bool:
        with socket.socket() as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", port)) == 0

    log = _stops_when_stdin_closes(["-m", "central"], env, listening, tmp_path)
    assert "Traceback" not in log
