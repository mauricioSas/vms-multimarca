"""Ciclo de vida de una actualización entre el actualizador (Python), `vmshost` (Rust) y el panel central.

Un caso por hallazgo de la revisión del frente «actualizador» (A1, A2, A4 y los bajos). Las pruebas marcadas
con `real_vmshost` usan el `vmshost` compilado de este repositorio (`native/target/debug/vmshost`, o
`VMS_TEST_VMSHOST`): se saltan si no está compilado (`cd native && cargo build -p vmshost`). Lo mismo, sin
binario, lo cubren las pruebas de `cargo test -p vmshost` (`host::tests`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from vms_updater._atomic import atomic_write_json
from vms_updater.pointer import PointerStore

from .conftest import ROOT, Site

APP_RESTART = ["VMSBackend", "VMSHeartbeat"]          # los de la sede de prueba que reinicia «app»


def _vmshost() -> Path | None:
    env = os.environ.get("VMS_TEST_VMSHOST")
    if env:
        return Path(env)
    exe = ROOT / "native" / "target" / "debug" / ("vmshost.exe" if sys.platform == "win32" else "vmshost")
    return exe if exe.is_file() else None


real_vmshost = pytest.mark.skipif(_vmshost() is None or sys.platform == "win32",
                                  reason="sin native/target/debug/vmshost (cargo build -p vmshost) o en Windows")


def _ptr_raw(site: Site) -> dict[str, Any]:
    return dict(json.loads(site.layout.pointer_file.read_text(encoding="utf-8")))


# =========================================================================== A1: el motor no se corta
def test_app_update_tells_vmshost_which_services_it_restarts(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    raw = _ptr_raw(site)
    assert raw["active"] == "2.1.0" and not raw["trial"]
    assert raw["restart"] == APP_RESTART, raw          # sin VMSEngine: vmshost no lo relanza
    assert site.running()["VMSEngine"] == "2.0.0"


def test_rollback_keeps_the_restart_list(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), broken=True)
    out = site.engine().check()
    assert out.result == "update_failed"
    raw = _ptr_raw(site)
    assert raw["active"] == "2.0.0" and raw["restart"] == APP_RESTART


def test_engine_update_restarts_only_the_engine(site: Site) -> None:
    site.factory.release("2.1.0", comps=("engine",))
    assert site.engine().check().result == "update_ok"
    assert _ptr_raw(site)["restart"] == ["VMSEngine"]


def test_cleanup_keeps_the_version_a_service_really_runs(site: Site) -> None:
    """La retención no borra la carpeta de un servicio que no se reinició, aunque `running.json` se pierda:
    `vmsctl run` publica la versión que ejecuta en `logs/status-<Servicio>.json`."""
    eng = site.engine()
    site.layout.logs_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(site.layout.logs_dir / "status-VMSEngine.json",
                      {"schema": 1, "service": "VMSEngine", "version": "2.0.0", "state": "running",
                       "vmsctl_pid": 1, "updated_unix": int(time.time())})
    for v in ("2.1.0", "2.2.0", "2.3.0", "2.4.0"):
        site.factory.release(v, comps=("app",))
        (site.layout.updater_data / "running.json").unlink(missing_ok=True)
        assert eng.check().result == "update_ok", v
    left = sorted(p.name for p in site.layout.versions_dir.iterdir() if p.is_dir())
    assert "2.0.0" in left, left                        # el motor sigue ahí
    assert "2.1.0" not in left and "2.2.0" not in left, left


@real_vmshost
def test_real_vmshost_keeps_the_engine_running_through_an_app_update(site: Site, tmp_path: Path) -> None:
    """Repro del revisor (repro_engine_restart.py) con el vmshost real: «arranca motor 2.0.0 → PARA motor
    2.0.0 → arranca motor 2.1.0» al pasar el actualizador por `switched`."""
    exe = _vmshost()
    assert exe is not None
    L = site.layout
    events = tmp_path / "events.log"
    for v in ("2.0.0", "2.1.0"):
        b = L.version_dir(v) / "bin"
        b.mkdir(parents=True, exist_ok=True)
        (b / "vmsctl").write_text(f'#!/bin/sh\necho "arranca motor {v}" >> {events}\n'
                                  f'while read l; do :; done\necho "PARA motor {v}" >> {events}\n')
        os.chmod(b / "vmsctl", 0o755)
    host = subprocess.Popen([str(exe), "service", "--name", "VMSEngine", "--foreground", "--install-dir",
                             str(L.install), "--data-dir", str(L.data)], stdin=subprocess.PIPE)
    try:
        _wait(lambda: events.is_file() and "arranca motor 2.0.0" in events.read_text())
        # paso `switched` de una actualización solo de `app`
        PointerStore(L.pointer_file).switch("2.1.0", trial=True, restart=APP_RESTART)
        time.sleep(2.5)
        PointerStore(L.pointer_file).confirm()
        time.sleep(1.5)
    finally:
        assert host.stdin is not None
        host.stdin.close()
        host.wait(timeout=30)
    lines = events.read_text().splitlines()
    assert lines == ["arranca motor 2.0.0", "PARA motor 2.0.0"], lines   # solo la parada final del arrancador


def _wait(cond: Any, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("no se cumplió a tiempo")


# =========================================================================== A2: la vuelta atrás de vmshost
def _vmshost_rolls_back(site: Site, reason: str = "nadie confirmó 2.1.0 en 30 min") -> None:
    """Lo que hace `StateDir::roll_back_version` (Rust): puntero a la buena y `state/host-rollback.json`."""
    ps = PointerStore(site.layout.pointer_file)
    p = ps.read()
    assert p is not None and p.trial
    ps.write(p.model_copy(update={"active": "2.0.0", "previous": p.active, "trial": False,
                                  "trial_since_unix": None}))
    atomic_write_json(site.layout.state_dir / "host-rollback.json",
                      {"schema": 1, "from": p.active, "to": "2.0.0", "reason": reason,
                       "at_unix": int(time.time()), "by": "vmshost"})


def _cut_in_verifying(site: Site) -> None:
    from vms_updater.journal import SimulatedCrash

    from .conftest import crash_at
    site.factory.release("2.1.0", comps=("app",))
    with pytest.raises(SimulatedCrash):
        site.engine(fault_hook=crash_at("verifying", "before")).check()     # corte de luz en `verifying`
    assert json.loads(site.config_bytes()).get("migrated_by") == "2.1.0"
    assert site.pointer().active == "2.1.0" and site.pointer().trial


def _assert_rolled_back_and_blacklisted(site: Site, out: Any) -> None:
    from vms_updater.state_files import Blacklist
    assert out is not None and out.result == "update_failed", out
    assert "antes de cambiar de versión" not in out.message_es, out.message_es
    assert "vmshost" in out.message_es, out.message_es
    assert "migrated_by" not in json.loads(site.config_bytes()), "se restaura la config de antes de migrar"
    assert Blacklist(site.layout.blacklist_file).contains("2.1.0")
    j = site.journal()
    assert j is not None and j.state == "rolled_back" and not j.aborted and j.last_good == "2.0.0"
    assert site.pointer().active == "2.0.0" and not site.pointer().trial
    nxt = site.engine().check()
    assert site.pointer().active == "2.0.0" and nxt.result == "no_update", nxt   # no la reinstala


def test_recovery_sees_the_rollback_made_by_vmshost(site: Site) -> None:
    _cut_in_verifying(site)
    _vmshost_rolls_back(site)
    _assert_rolled_back_and_blacklisted(site, site.engine().startup())


def test_old_host_rollback_note_does_not_blame_an_update_that_never_switched(site: Site) -> None:
    """Un `host-rollback.json` de un intento anterior no convierte en «fallida» una actualización que se
    cortó antes de `switched` (el puntero nunca cambió): eso sigue siendo «interrumpida, se reintenta»."""
    from vms_updater.journal import SimulatedCrash
    from vms_updater.state_files import Blacklist

    from .conftest import crash_at
    atomic_write_json(site.layout.state_dir / "host-rollback.json",
                      {"schema": 1, "from": "2.1.0", "to": "2.0.0", "reason": "vieja", "at_unix": 1, "by": "vmshost"})
    site.factory.release("2.1.0", comps=("app",))
    with pytest.raises(SimulatedCrash):
        site.engine(fault_hook=crash_at("stopping", "after")).check()
    out = site.engine().startup()
    assert out is not None and "antes de cambiar de versión" in out.message_es
    assert not Blacklist(site.layout.blacklist_file).contains("2.1.0")


def test_rollback_request_file_is_gone(site: Site) -> None:
    """La ruta `state/rollback-request.json` no la escribía nadie: fuera (las peticiones van a
    `state/requests/` y las atiende el vmshost de VMSUpdater)."""
    assert not hasattr(site.layout, "rollback_request_file")
    assert not hasattr(site.engine(), "handle_rollback_request")


@real_vmshost
def test_real_vmshost_rollback_after_a_long_power_cut_is_seen_by_the_updater(site: Site) -> None:
    """Repro del revisor (pytests/test_rev_b4_b1.py) con el vmshost real de VMSUpdater."""
    exe = _vmshost()
    assert exe is not None
    _cut_in_verifying(site)
    L = site.layout
    ps = PointerStore(L.pointer_file)
    ptr = ps.read()
    assert ptr is not None
    ps.write(ptr.model_copy(update={"trial_since_unix": int(time.time()) - 3600}))   # equipo apagado > 30 min
    for v in ("2.0.0", "2.1.0"):                      # en macOS/Linux vmshost busca versions/<v>/bin/vmsctl
        (L.version_dir(v) / "bin" / "vmsctl").write_bytes(b"#!/bin/sh\n")
    slot = L.slot_dir("a")
    slot.mkdir(parents=True, exist_ok=True)
    (slot / "vmsctl").write_text("#!/bin/sh\nwhile read l; do :; done\n")
    os.chmod(slot / "vmsctl", 0o755)
    p = subprocess.Popen([str(exe), "service", "--name", "VMSUpdater", "--foreground", "--install-dir",
                          str(L.install), "--data-dir", str(L.data)], stdin=subprocess.PIPE)
    try:
        _wait(lambda: (q := ps.read()) is not None and q.active == "2.0.0")
    finally:
        assert p.stdin is not None
        p.stdin.close()
        p.wait(timeout=30)
    assert (L.state_dir / "host-rollback.json").is_file()
    _assert_rolled_back_and_blacklisted(site, site.engine().startup())
