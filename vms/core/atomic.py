"""Escritura atómica de archivos (CONTRATO §13.5; gemelo de `vms_common::atomic_write` en Rust).

Temporal `<nombre>.tmp-<pid>` en la misma carpeta → flush + fsync → sustitución atómica:
`MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)` en Windows (el cambio de nombre llega al
disco antes de volver) y `os.replace` + fsync de la carpeta en POSIX. Un corte de luz deja el archivo
anterior o el nuevo, nunca uno a medias. En Windows un antivirus o un lector pueden bloquear el archivo un
instante: se reintenta.

Se usa para `active.json`, `journal.json`, `config.json`, `users.json`, `mediamtx.yml` y
`public-status.json`.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("vms.core.atomic")

REPLACE_RETRIES = 20
RETRY_SLEEP_S = 0.1
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


def _fsync_dir(directory: Path) -> None:
    """Persiste el cambio de nombre (POSIX). En Windows no hace falta: lo hace MOVEFILE_WRITE_THROUGH."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        log.debug("No se pudo abrir %s para fsync: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # algunos sistemas de archivos no lo admiten en carpetas
        log.debug("fsync de la carpeta %s no admitido: %s", directory, exc)
    finally:
        os.close(fd)


def temp_path(path: Path) -> Path:
    """Ruta del temporal que usa `atomic_write_bytes` para `path`."""
    return path.with_name(f"{path.name}.tmp-{os.getpid()}")


def write_temp(path: Path, data: bytes) -> Path:
    """Primer paso: escribe y persiste el temporal. Público para poder inyectar un fallo entre los dos pasos."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path(path)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def _replace_once(tmp: Path, dst: Path) -> None:
    if sys.platform == "win32":  # pragma: no cover - solo Windows (lo prueba el job de Windows de B1)
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move.restype = ctypes.c_int
        if not move(str(tmp), str(dst), _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
            # WinError(5) es PermissionError: así entra en los reintentos de abajo.
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(tmp, dst)
    _fsync_dir(dst.parent)


def replace(tmp: Path, dst: Path) -> None:
    """Segundo paso: sustituye `dst` por `tmp` de forma atómica (con reintentos si está bloqueado)."""
    last_exc: OSError | None = None
    for _ in range(REPLACE_RETRIES):
        try:
            _replace_once(tmp, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(RETRY_SLEEP_S)
        except OSError:
            _discard(tmp)
            raise
    _discard(tmp)
    assert last_exc is not None
    raise last_exc


def _discard(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError as exc:
        log.debug("No se pudo borrar el temporal %s: %s", tmp, exc)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    replace(write_temp(path, data), path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))
