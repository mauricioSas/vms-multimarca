"""Escritura atómica de archivos.

Se escribe en un temporal de la misma carpeta, se hace fsync y se sustituye con os.replace
(atómico en NTFS y en POSIX). Un corte de luz deja el archivo anterior o el nuevo, nunca uno
a medias. En Windows un antivirus puede bloquear el archivo un instante: se reintenta.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path


def _fsync_dir(directory: Path) -> None:
    """Persiste el cambio de nombre (POSIX). En Windows NTFS no se puede abrir una carpeta así: no hace falta."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        logging.getLogger("vms.core.atomic").debug("No se pudo abrir %s para fsync: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # algunos sistemas de archivos no lo admiten en carpetas
        logging.getLogger("vms.core.atomic").debug("fsync de la carpeta %s no admitido: %s", directory, exc)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    last_exc: OSError | None = None
    for _ in range(20):
        try:
            os.replace(tmp, path)
            _fsync_dir(path.parent)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.1)
    try:
        tmp.unlink()
    except OSError:
        pass
    assert last_exc is not None
    raise last_exc
