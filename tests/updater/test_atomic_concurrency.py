"""Escrituras a la vez desde dos hilos del mismo actualizador (CONTRATO §13.5 y §15.6).

`VMSUpdater.log` del PC de la tienda (08/10 11:08:13): «No se pudo escribir public-status.json: MoveFileExW falló
con el código 2». El bucle principal (`Engine._set_status`) y el hilo de la tubería de control (`on_problem`,
`hold`) escribían el mismo archivo con el mismo temporal `<nombre>.tmp-<pid>`: uno vaciaba o renombraba el del
otro. Además, leer-cambiar-reescribir sin cerrojo hacía que uno pisara los campos del otro.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from vms_updater import _atomic, engine as engine_mod, state_files
from vms_updater._atomic import atomic_write_json
from vms_updater.control_pipe import handle_request
from vms_updater.state_files import StatusFile, read_local_config

from .conftest import Site


def _temps(d: Path) -> list[str]:
    return [p.name for p in d.iterdir() if ".tmp-" in p.name]


def test_two_writers_of_one_file_do_not_share_the_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A escribe su temporal y, justo antes de renombrarlo, B escribe el mismo archivo de principio a fin."""
    p = tmp_path / "public-status.json"
    real = _atomic._replace
    a_ready, b_done = threading.Event(), threading.Event()

    def rename_after_b(src: Path, dst: Path) -> None:
        if threading.current_thread().name == "A" and not b_done.is_set():
            a_ready.set()
            b_done.wait(5)
        real(src, dst)

    monkeypatch.setattr(_atomic, "_replace", rename_after_b)
    errors: list[OSError] = []

    def writer_a() -> None:
        try:
            atomic_write_json(p, {"de": "A"})
        except OSError as exc:
            errors.append(exc)

    ta = threading.Thread(target=writer_a, name="A")
    ta.start()
    assert a_ready.wait(5)
    atomic_write_json(p, {"de": "B"})
    b_done.set()
    ta.join(5)
    assert errors == [], errors          # antes: FileNotFoundError(2, 'MoveFileExW falló con el código 2')
    assert json.loads(p.read_text(encoding="utf-8")) == {"de": "A"}     # gana el último en renombrar
    assert _temps(tmp_path) == []


def test_status_updates_from_two_threads_keep_both_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La comprobación (bucle) y la tubería (`hold`) cambian campos distintos a la vez: no se pierde ninguno."""
    p = tmp_path / "public-status.json"
    real = state_files.atomic_write_json
    a_read = threading.Event()

    def slow_for_a(path: Path, data: object) -> None:
        if threading.current_thread().name == "A":
            a_read.set()
            time.sleep(0.5)                  # A ya leyó el estado; B escribe mientras tanto
        real(path, data)

    monkeypatch.setattr(state_files, "atomic_write_json", slow_for_a)
    ta = threading.Thread(target=lambda: StatusFile(p).update(last_result="error", message_es="sin fuente"),
                          name="A")
    ta.start()
    assert a_read.wait(5)
    StatusFile(p).update(hold=True)
    ta.join(5)
    st = json.loads(p.read_text(encoding="utf-8"))
    assert st["last_result"] == "error" and st["hold"] is True, st


def test_many_concurrent_status_updates_never_fail_or_corrupt(tmp_path: Path) -> None:
    """Prueba de carga (en Linux rápido puede pasar sin el arreglo; las dos de arriba son las deterministas)."""
    p = tmp_path / "public-status.json"
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(field: str) -> None:
        barrier.wait()
        for i in range(150):
            try:
                StatusFile(p).update(**{field: f"{field}-{i}"})
            except OSError as exc:
                errors.append(repr(exc))

    ts = [threading.Thread(target=worker, args=(f,)) for f in ("message_es", "control_error")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    st = json.loads(p.read_text(encoding="utf-8"))          # JSON válido, nunca una mezcla de dos escrituras
    assert errors == [], errors[:3]
    assert st["message_es"] == "message_es-149" and st["control_error"] == "control_error-149"
    assert _temps(tmp_path) == []


def test_failed_rename_never_leaves_the_temp_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "updater.json"
    atomic_write_json(p, {"v": 1})

    def gone(src: Path, dst: Path) -> None:
        raise OSError(2, "MoveFileExW falló con el código 2", str(dst))

    monkeypatch.setattr(_atomic, "_replace", gone)
    with pytest.raises(OSError):
        atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 1}
    assert _temps(tmp_path) == []


def test_hold_from_the_pipe_is_kept_while_the_panel_directive_is_applied(site: Site,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """`updater.json`: el panel central cambia el canal (bucle principal) mientras alguien pide «retener» por la
    tubería. Antes, el que escribía el último borraba el cambio del otro."""
    eng = site.engine()
    atomic_write_json(site.layout.directive_file, {"channel": "pilot", "received": "2026-10-08T09:00:00Z"})
    real = engine_mod.write_local_config
    a_read = threading.Event()

    def slow_write(path: Path, cfg: object) -> None:
        a_read.set()
        time.sleep(0.5)                      # la directiva ya leyó updater.json; llega el «hold»
        real(path, cfg)                      # type: ignore[arg-type]

    monkeypatch.setattr(engine_mod, "write_local_config", slow_write)
    ta = threading.Thread(target=lambda: eng._apply_directive(eng.config()), name="A")
    ta.start()
    assert a_read.wait(5)
    assert handle_request(eng, {"cmd": "hold", "on": True}) == {"ok": True}
    ta.join(5)
    cfg = read_local_config(site.layout.local_config_file)
    assert cfg.channel == "pilot" and cfg.hold is True, cfg
