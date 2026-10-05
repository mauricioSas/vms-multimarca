"""Escritura atómica (CONTRATO §13.5) sin depender del paquete `vms`.

Temporal `<nombre>.tmp-<pid>` en la misma carpeta → flush + fsync → sustitución atómica. En Windows se usa
`MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)`, que no devuelve hasta que el cambio de
nombre está en disco; en POSIX, `os.replace` + fsync de la carpeta. Si un antivirus bloquea el archivo un
instante, se reintenta. Un corte de luz deja el archivo anterior o el nuevo, nunca uno a medias.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("vms_updater.atomic")

_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


def _replace(src: Path, dst: Path) -> None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        move = k32.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(src), str(dst), _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err in (5, 32, 33):   # acceso denegado / compartido / bloqueado (antivirus)
                raise PermissionError(err, f"MoveFileExW falló con el código {err}", str(dst))
            raise OSError(err, f"MoveFileExW falló con el código {err}", str(dst))
        return
    os.replace(src, dst)


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fsync_dirs(root: Path) -> None:
    """fsync de `root` y de todas sus subcarpetas (POSIX). En NTFS las entradas de carpeta van en el
    registro de transacciones del sistema de archivos; lo que hay que forzar son los datos de cada archivo."""
    if os.name == "nt":
        return
    root = Path(root)
    for d in [root, *(p for p in root.rglob("*") if p.is_dir() and not p.is_symlink())]:
        _fsync_dir(d)


def write_durable(path: Path, data: bytes) -> None:
    """Escribe un archivo nuevo y no vuelve hasta que sus datos están en disco (flush + fsync)."""
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def copy_durable(src: Path, dst: Path) -> None:
    """Copia con datos en disco al volver (y fechas y permisos como `shutil.copy2`)."""
    import shutil

    with open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)
        fo.flush()
        os.fsync(fo.fileno())
    shutil.copystat(src, dst)


def rename_durable(src: Path, dst: Path, *, retries: int = 20) -> None:
    """Renombra un archivo o carpeta (el destino no debe existir) y no vuelve hasta que el cambio está en
    disco: `MoveFileExW(MOVEFILE_WRITE_THROUGH)` en Windows; `os.rename` + fsync de la carpeta en POSIX."""
    src, dst = Path(src), Path(dst)
    last: OSError | None = None
    for _ in range(retries):
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
                move = k32.MoveFileExW
                move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
                move.restype = wintypes.BOOL
                if not move(str(src), str(dst), _MOVEFILE_WRITE_THROUGH):
                    err = ctypes.get_last_error()  # type: ignore[attr-defined]
                    if err in (5, 32, 33):
                        raise PermissionError(err, f"MoveFileExW falló con el código {err}", str(dst))
                    raise OSError(err, f"MoveFileExW falló con el código {err}", str(dst))
            else:
                os.rename(src, dst)
                _fsync_dir(dst.parent)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.1)
    assert last is not None
    raise last


def atomic_write_bytes(path: Path, data: bytes, *, retries: int = 20) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    last: OSError | None = None
    for _ in range(retries):
        try:
            _replace(tmp, path)
            _fsync_dir(path.parent)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.1)
    try:
        tmp.unlink()
    except OSError:
        pass
    assert last is not None
    raise last


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def read_json(path: Path) -> Any | None:
    """JSON de un archivo, o None si falta o no es JSON válido (se registra)."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("No se pudo leer %s: %s", path, exc)
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        log.warning("%s no es JSON válido: %s", path, exc)
        return None


def cleanup_temporaries(directory: Path) -> int:
    """Borra temporales `*.tmp-<pid>` que dejó un corte de luz. Devuelve cuántos borró."""
    n = 0
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return 0
    for p in entries:
        if ".tmp-" in p.name and p.is_file():
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n
