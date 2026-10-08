"""Parada ordenada del servicio `VMSUpdater` bajo `vmsctl run` (CONTRATO §14.1).

En Windows, la única orden de parada que manda `vmsctl run` es cerrar la entrada estándar del proceso (con
`VMS_STOP_ON_STDIN_EOF=1`): ni SIGTERM ni Ctrl+Break. El actualizador solo instalaba manejadores de señales, así
que seguía dormido y vmsctl lo mataba a los 10 s en cada parada, reinicio o apagado del equipo
(`VMSUpdater.log` del PC de la tienda, 06-08/10: «[vmsctl] no paró en 10 s: se fuerza»).
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from vms_updater import service

UPDATER_DIR = Path(__file__).resolve().parents[2] / "updater"


def test_closing_stdin_sets_the_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    class ClosedStdin:
        buffer = io.BytesIO(b"")     # fin de la entrada nada más empezar: vmsctl ya la cerró

    monkeypatch.setattr(sys, "stdin", ClosedStdin())
    monkeypatch.setattr(service.signal, "signal", lambda *_a: None)     # no tocar las señales de pytest
    monkeypatch.setenv("VMS_STOP_ON_STDIN_EOF", "1")
    stop = threading.Event()
    service.install_signal_handlers(stop)
    assert stop.wait(5), "el actualizador no se entera de que vmsctl pide la parada"


def test_without_vmsctl_stdin_is_not_watched(monkeypatch: pytest.MonkeyPatch) -> None:
    """En una consola (`vms_updater run` a mano, sin la variable) cerrar la entrada no para nada."""
    class ClosedStdin:
        buffer = io.BytesIO(b"")

    monkeypatch.setattr(sys, "stdin", ClosedStdin())
    monkeypatch.setattr(service.signal, "signal", lambda *_a: None)
    monkeypatch.delenv("VMS_STOP_ON_STDIN_EOF", raising=False)
    stop = threading.Event()
    service.install_signal_handlers(stop)
    assert not stop.wait(0.3)


# El proceso hijo hace lo que `vmsctl run --service VMSUpdater`: `python -m vms_updater run`. La tubería de
# control se cambia por una que no hace nada: su nombre de verdad es del servicio instalado en este PC.
CHILD = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    import vms_updater.control_pipe as cp

    class NoPipe:
        def start(self):
            pass

        def stop(self):
            pass

    cp.make_server = lambda engine: NoPipe()
    from vms_updater.__main__ import main
    sys.exit(main(["run"]))
""")


def test_run_stops_in_order_when_vmsctl_closes_stdin(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VMS_")}
    env.update(VMS_DATA_DIR=str(tmp_path / "data"), VMS_INSTALL_DIR=str(tmp_path / "install"),
               VMS_STOP_ON_STDIN_EOF="1", VMS_UPDATER_VMSCTL=str(tmp_path / "no-existe.exe"),
               VMS_UPDATER_TEST_HOOKS="1", VMS_UPDATER_TEST_FAKE_SYSTEM="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONUTF8="1")
    err = tmp_path / "stderr.txt"
    status = tmp_path / "data" / "updater" / "public-status.json"
    with open(err, "wb") as ferr:
        proc = subprocess.Popen([sys.executable, "-c", CHILD, str(UPDATER_DIR)], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=ferr, env=env)
        try:
            # Sin fuente de actualizaciones, la comprobación del arranque acaba enseguida en «error»: a partir
            # de ahí el servicio duerme en su bucle, como en el PC de la tienda.
            deadline = time.monotonic() + 60
            while not (status.is_file() and "last_result" in status.read_text(encoding="utf-8", errors="replace")):
                assert proc.poll() is None, err.read_text(encoding="utf-8", errors="replace")[-2000:]
                assert time.monotonic() < deadline, "el actualizador no llegó a comprobar"
                time.sleep(0.1)
            assert proc.stdin is not None
            t0 = time.monotonic()
            proc.stdin.close()                       # lo que hace vmsctl run al parar el servicio
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail("el actualizador no paró en 5 s tras cerrar su entrada estándar (vmsctl lo mataría)")
            elapsed = time.monotonic() - t0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    log = err.read_text(encoding="utf-8", errors="replace")
    assert code == 0, log[-2000:]
    assert elapsed < 5
    assert "Parada pedida por el servicio" in log
