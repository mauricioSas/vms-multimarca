"""Entrada estándar privada para la señal de parada de vmsctl (cerrar stdin = parar). Dueño: B1.

En Windows, mientras un hilo está bloqueado en un ReadFile síncrono sobre la tubería de stdin, cualquier otra
operación sobre ese mismo objeto de archivo espera a que termine; por ejemplo GetFileType(STD_INPUT_HANDLE), que
hacen la CRT o algunas DLL al cargarse. Con el loader lock tomado, eso dejaba colgado el backend entero al importar
numpy/cv2 (CI de Windows, run 42: el volcado de faulthandler mostraba el hilo en numpy/__init__ y el bucle esperando
a que arrancara un hilo nuevo). Aquí el vigilante se queda con un duplicado de la tubería y el STD_INPUT_HANDLE del
proceso pasa a apuntar a NUL: nadie más puede tropezar con la lectura pendiente.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from typing import BinaryIO

log = logging.getLogger("vms.core.stdin_stop")

# Lo fija `vmsctl run`: cerrar la entrada estándar es la orden de parada. En Windows es la ÚNICA que llega (no
# hay SIGTERM y un servicio no tiene consola para Ctrl+C/Ctrl+Break): quien no la vigila, muere a la fuerza.
STOP_ON_STDIN_EOF = "VMS_STOP_ON_STDIN_EOF"
STD_INPUT_HANDLE = -10


def stop_on_stdin_eof() -> bool:
    """¿Lo ha lanzado `vmsctl run` (`VMS_STOP_ON_STDIN_EOF=1`)?"""
    return os.environ.get(STOP_ON_STDIN_EOF) == "1"


def watch_stdin_eof(on_eof: Callable[[], None], stream: BinaryIO | None = None) -> threading.Thread:
    """Hilo que llama a `on_eof` cuando se cierra la entrada estándar (`stream` solo en pruebas). `on_eof` corre
    en ese hilo: para tocar un bucle asyncio, `loop.call_soon_threadsafe`."""
    src = stream if stream is not None else private_stdin()

    def watch() -> None:
        try:
            while src is not None and src.read(4096):
                pass
        except (OSError, ValueError):
            pass
        log.info("Parada pedida por el servicio (entrada estándar cerrada)")
        on_eof()

    t = threading.Thread(target=watch, daemon=True, name="stdin-eof-stop")
    t.start()
    return t


def interrupt_main() -> None:
    """Simula Ctrl+C en el hilo principal: un servidor uvicorn para de forma ordenada, igual que en una consola."""
    try:
        signal.raise_signal(signal.SIGINT)
    except (OSError, ValueError):
        import _thread

        _thread.interrupt_main()


def private_stdin() -> BinaryIO | None:
    """Flujo binario de la entrada estándar solo para el vigilante de parada (None si no hay entrada)."""
    if sys.stdin is None:
        return None
    if sys.platform == "win32":  # pragma: no cover - solo Windows (tests/core/test_stdin_stop.py)
        return _detach_windows()
    return sys.stdin.buffer


if sys.platform == "win32":  # pragma: no cover - solo Windows (tests/core/test_stdin_stop.py)
    def _detach_windows() -> BinaryIO | None:
        import ctypes
        import msvcrt

        try:
            fd = sys.stdin.fileno()
        except (OSError, ValueError, AttributeError):
            return sys.stdin.buffer
        own = os.dup(fd)                             # el mismo extremo de la tubería, solo para el vigilante
        nul = os.open(os.devnull, os.O_RDONLY)
        os.dup2(nul, fd)                             # el descriptor 0 de la CRT pasa a ser NUL…
        os.close(nul)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetStdHandle(STD_INPUT_HANDLE, ctypes.c_void_p(msvcrt.get_osfhandle(fd)))   # …y el de Win32 también
        sys.stdin = open(fd, "r", encoding="utf-8", closefd=False)   # noqa: SIM115 - vive lo que el proceso
        return open(own, "rb", buffering=0)          # noqa: SIM115 - lo cierra el vigilante al ver el fin
