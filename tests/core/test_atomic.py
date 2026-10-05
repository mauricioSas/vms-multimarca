"""`atomic_write` (CONTRATO §13.5): fallo inyectado entre escribir y renombrar, reintentos y Windows real."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from vms.core import atomic


def test_writes_and_replaces_without_leaving_the_temp(tmp_path: Path) -> None:
    p = tmp_path / "state" / "active.json"
    atomic.atomic_write_text(p, '{"active":"1.0.0"}')
    atomic.atomic_write_text(p, '{"active":"2.0.0"}')
    assert p.read_text(encoding="utf-8") == '{"active":"2.0.0"}'
    assert not atomic.temp_path(p).exists()
    assert atomic.temp_path(p).name == f"active.json.tmp-{os.getpid()}"  # mismo nombre que en Rust


def test_crash_between_write_and_rename_keeps_the_old_file(tmp_path: Path) -> None:
    """Fallo inyectado: el proceso «muere» tras escribir el temporal y antes de renombrar."""
    p = tmp_path / "config.json"
    atomic.atomic_write_bytes(p, b"viejo")
    tmp = atomic.write_temp(p, b"nuevo-a-medias")
    assert p.read_bytes() == b"viejo"
    assert tmp.exists()
    # Al «reiniciar», el siguiente escritor pisa el temporal huérfano y termina bien
    atomic.atomic_write_bytes(p, b"nuevo")
    assert p.read_bytes() == b"nuevo"
    assert not tmp.exists()


def test_failure_in_the_rename_keeps_the_old_file_and_removes_the_temp(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "users.json"
    atomic.atomic_write_bytes(p, b"bueno")

    def boom(tmp: Path, dst: Path) -> None:
        raise OSError(28, "No queda espacio en el disco")

    monkeypatch.setattr(atomic, "_replace_once", boom)
    with pytest.raises(OSError):
        atomic.atomic_write_bytes(p, b"nunca")
    assert p.read_bytes() == b"bueno"
    assert not atomic.temp_path(p).exists()


def test_locked_file_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atomic, "RETRY_SLEEP_S", 0.001)
    real = atomic._replace_once
    calls = {"n": 0}

    def flaky(tmp: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "bloqueado por el antivirus")
        real(tmp, dst)

    monkeypatch.setattr(atomic, "_replace_once", flaky)
    p = tmp_path / "mediamtx.yml"
    atomic.atomic_write_text(p, "paths: {}\n")
    assert calls["n"] == 3 and p.read_text(encoding="utf-8") == "paths: {}\n"


def test_permanently_locked_file_gives_up_and_cleans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atomic, "RETRY_SLEEP_S", 0.001)

    def locked(tmp: Path, dst: Path) -> None:
        raise PermissionError(13, "bloqueado")

    monkeypatch.setattr(atomic, "_replace_once", locked)
    p = tmp_path / "x.json"
    with pytest.raises(PermissionError):
        atomic.atomic_write_bytes(p, b"x")
    assert not atomic.temp_path(p).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="MoveFileExW solo existe en Windows")
def test_windows_movefileex_waits_for_a_reader_to_close(tmp_path: Path) -> None:  # pragma: no cover
    """Un lector que tiene el archivo abierto sin FILE_SHARE_DELETE bloquea el reemplazo un momento."""
    p = tmp_path / "public-status.json"
    atomic.atomic_write_text(p, "uno")
    f = open(p, "rb")  # Python abre sin FILE_SHARE_DELETE
    threading.Timer(0.3, f.close).start()
    t0 = time.monotonic()
    atomic.atomic_write_text(p, "dos")
    assert p.read_text(encoding="utf-8") == "dos"
    assert time.monotonic() - t0 >= 0.2
