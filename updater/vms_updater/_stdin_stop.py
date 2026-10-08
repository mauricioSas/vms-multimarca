"""Parada pedida por `vmsctl run`: cierra la entrada estándar del proceso (`VMS_STOP_ON_STDIN_EOF=1`).

En Windows es la ÚNICA orden de parada que llega: vmsctl no manda SIGTERM ni Ctrl+Break (un servicio no tiene
consola). Sin este vigilante, `vms_updater run` seguía dormido en su espera y vmsctl lo mataba a los 10 s de
gracia en cada parada, reinicio o apagado del equipo («no paró en 10 s: se fuerza» en `VMSUpdater.log`).

Gemelo de `vms/core/stdin_stop.py`: la ranura del actualizador solo lleva `app\\vms_updater`, sin el paquete
`vms`. Igual que allí, en Windows el vigilante se queda con un duplicado de la tubería y el STD_INPUT_HANDLE del
proceso pasa a apuntar a NUL, para que nadie más tropiece con la lectura pendiente (ver allí por qué).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import BinaryIO

log = logging.getLogger("vms_updater.service")

STOP_ON_STDIN_EOF = "VMS_STOP_ON_STDIN_EOF"
STD_INPUT_HANDLE = -10


def private_stdin() -> BinaryIO | None:
    """Flujo binario de la entrada estándar solo para el vigilante de parada (None si no hay entrada)."""
    if sys.stdin is None:
        return None
    if sys.platform == "win32":  # pragma: no cover - solo Windows (tests/updater/test_service_stop.py)
        return _detach_windows()
    return sys.stdin.buffer


if sys.platform == "win32":  # pragma: no cover - solo Windows (tests/updater/test_service_stop.py)
    def _detach_windows() -> BinaryIO | None:
        import ctypes
        import msvcrt

        try:
            fd = sys.stdin.fileno()
        except (OSError, ValueError, AttributeError):
            return getattr(sys.stdin, "buffer", None)
        own = os.dup(fd)                             # el mismo extremo de la tubería, solo para el vigilante
        nul = os.open(os.devnull, os.O_RDONLY)
        os.dup2(nul, fd)                             # el descriptor 0 de la CRT pasa a ser NUL…
        os.close(nul)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetStdHandle(STD_INPUT_HANDLE, ctypes.c_void_p(msvcrt.get_osfhandle(fd)))   # …y el de Win32 también
        sys.stdin = open(fd, "r", encoding="utf-8", closefd=False)   # noqa: SIM115 - vive lo que el proceso
        return open(own, "rb", buffering=0)          # noqa: SIM115 - lo cierra el vigilante al ver el fin


def watch_stdin_eof(stop: threading.Event, stream: BinaryIO | None = None) -> threading.Thread:
    """Hilo que pone `stop` cuando se cierra la entrada estándar (`stream` solo en pruebas)."""
    src = stream if stream is not None else private_stdin()

    def watch() -> None:
        try:
            while src is not None and src.read(4096):
                pass
        except (OSError, ValueError):
            pass
        log.info("Parada pedida por el servicio (entrada estándar cerrada)")
        stop.set()

    t = threading.Thread(target=watch, daemon=True, name="stdin-eof-stop")
    t.start()
    return t
