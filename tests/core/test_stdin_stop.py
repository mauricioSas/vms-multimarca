"""Vigilante de parada por stdin sin colgar el proceso (CI de Windows, run 42).

Con un hilo bloqueado leyendo la tubería de stdin, en Windows GetFileType(STD_INPUT_HANDLE) esperaba a que la
lectura terminase; al importar numpy/cv2 con el loader lock tomado, el backend se quedaba colgado para siempre.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from vms.core.stdin_stop import private_stdin

CHILD = textwrap.dedent("""
    import ctypes, sys, threading, time
    from vms.core.stdin_stop import private_stdin

    stdin = private_stdin()
    threading.Thread(target=lambda: stdin.read(4096), daemon=True).start()   # lectura pendiente, como vmsctl
    time.sleep(0.5)
    k32 = ctypes.WinDLL("kernel32")
    k32.GetStdHandle.restype = ctypes.c_void_p
    k32.GetFileType.argtypes = [ctypes.c_void_p]
    done = threading.Event()
    def probe():
        k32.GetFileType(k32.GetStdHandle(-10))     # lo que hacen la CRT y algunas DLL al cargarse
        import numpy                                # lo que colgaba el backend
        done.set()
    threading.Thread(target=probe, daemon=True).start()
    print("ok" if done.wait(20) else "colgado", flush=True)
""")


@pytest.mark.skipif(sys.platform != "win32", reason="bloqueo de E/S síncrona de Windows")
def test_pending_stdin_read_does_not_block_std_input_users() -> None:
    proc = subprocess.Popen([sys.executable, "-c", CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert out.strip() == "ok", out + err


def test_without_stdin_there_is_nothing_to_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    assert private_stdin() is None
