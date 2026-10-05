"""Entrada estándar privada para la señal de parada de vmsctl (cerrar stdin = parar). Dueño: B1.

En Windows, mientras un hilo está bloqueado en un ReadFile síncrono sobre la tubería de stdin, cualquier otra
operación sobre ese mismo objeto de archivo espera a que termine; por ejemplo GetFileType(STD_INPUT_HANDLE), que
hacen la CRT o algunas DLL al cargarse. Con el loader lock tomado, eso dejaba colgado el backend entero al importar
numpy/cv2 (CI de Windows, run 42: el volcado de faulthandler mostraba el hilo en numpy/__init__ y el bucle esperando
a que arrancara un hilo nuevo). Aquí el vigilante se queda con un duplicado de la tubería y el STD_INPUT_HANDLE del
proceso pasa a apuntar a NUL: nadie más puede tropezar con la lectura pendiente.
"""
from __future__ import annotations

import os
import sys
from typing import BinaryIO

STD_INPUT_HANDLE = -10


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
