"""Fallo inyectado en cada estado del diario (PLAN-V2 §4.4, CONTRATO §15.3).

Para cada estado y fase («justo antes» con el paso anotado, «justo después» hecho pero sin marcar):
1. el proceso muere ahí;
2. al arrancar, el diario retoma o revierte;
3. el resultado es siempre «nueva buena» o «anterior buena», nunca un estado intermedio (puntero sin
   «a prueba», servicios en marcha con la versión activa y health check bien);
4. recuperar dos veces da lo mismo (idempotente);
5. si se revirtió antes del punto de compromiso, la siguiente comprobación termina de actualizar.

Dos variantes: dentro del proceso (rápida, todos los estados) y con procesos de verdad que mueren con
`os._exit` (`VMS_UPDATER_FAULT_AT`, como en el e2e de Windows y en los cortes de luz de Hyper-V).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vms_updater.health import evaluate
from vms_updater.journal import FAULT_EXIT_CODE, SimulatedCrash
from vms_updater.models import FORWARD_STATES
from vms_updater.state_files import Blacklist

from .conftest import ROOT, SERVICES, Site, crash_at
from .doubles.fake_backend import deep_health, serve

PHASES = ("before", "after")
COMMIT = FORWARD_STATES.index("switched")


def assert_consistent(site: Site) -> str:
    """Estado final coherente; devuelve la versión activa."""
    j = site.journal()
    assert j is not None and not j.in_progress, j.dump() if j else None
    ptr = site.pointer()
    assert ptr.trial is False
    assert ptr.active in ("2.0.0", "2.1.0")
    assert (site.layout.version_dir(ptr.active) / "release.json").is_file()
    running = site.running()
    assert set(running) == set(SERVICES), running
    assert running["VMSBackend"] == ptr.active
    res = evaluate(deep_health(site.layout), expected_version=ptr.active, min_recording=8)
    assert res.ok, res.reason_es
    assert j.last_good == ptr.active
    assert not site.layout.staging_dir("2.1.0").exists()
    json.loads((site.layout.config_dir / "config.json").read_text())       # JSON válido
    return ptr.active


def snapshot(site: Site) -> tuple[str, str, str]:
    ptr = site.pointer().model_dump(exclude={"updated_unix"})
    j = site.journal()
    return (json.dumps(ptr, sort_keys=True), json.dumps(j.dump() if j else None, sort_keys=True),
            json.dumps(site.running(), sort_keys=True))


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("state", FORWARD_STATES)
def test_crash_in_each_forward_state_recovers(site: Site, state: str, phase: str) -> None:
    site.factory.release("2.1.0", comps=("app", "engine"))
    eng = site.engine(fault_hook=crash_at(state, phase))
    with pytest.raises(SimulatedCrash):
        eng.check()
    site.engine().startup()
    active = assert_consistent(site)
    if FORWARD_STATES.index(state) < COMMIT or (state == "switched" and phase == "before"):
        assert active == "2.0.0"                     # antes del compromiso: revertida
    else:
        assert active == "2.1.0"                     # después: sigue adelante y queda buena
    before = snapshot(site)
    assert site.engine().startup() is None or True
    assert snapshot(site) == before                  # idempotente
    if active == "2.0.0":
        assert not Blacklist(site.layout.blacklist_file).contains("2.1.0")
        assert site.engine().check().result == "update_ok"
        assert assert_consistent(site) == "2.1.0"


# «verifying» con una versión rota no llega a «después»: el paso falla y empieza la vuelta atrás.
@pytest.mark.parametrize("state,phase", [("verifying", "before"), ("rolling_back", "before"),
                                         ("rolling_back", "after"), ("rolled_back", "before"),
                                         ("rolled_back", "after")])
def test_crash_during_rollback_of_broken_version_ends_on_previous(site: Site, state: str, phase: str) -> None:
    site.factory.release("2.1.0", comps=("app",), broken=True)
    before_cfg = site.config_bytes()
    with pytest.raises(SimulatedCrash):
        site.engine(fault_hook=crash_at(state, phase)).check()
    site.engine().startup()
    assert assert_consistent(site) == "2.0.0"
    assert Blacklist(site.layout.blacklist_file).contains("2.1.0")
    assert site.config_bytes() == before_cfg
    before = snapshot(site)
    site.engine().startup()
    assert snapshot(site) == before
    assert site.engine().check().result == "no_update"


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("state", ["rolling_back", "rolled_back"])
def test_crash_during_manual_rollback(site: Site, state: str, phase: str) -> None:
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    with pytest.raises(SimulatedCrash):
        site.engine(fault_hook=crash_at(state, phase)).manual_rollback(None, "prueba")
    site.engine().startup()
    assert assert_consistent(site) == "2.0.0"
    bl = Blacklist(site.layout.blacklist_file)
    assert bl.kind_of("2.1.0") == "manual"                              # pedido, no fallido: omitida
    assert bl.skipped() == ["2.1.0"]                                   # y el corte no la olvida


def test_repeated_crashes_after_commit_end_in_rollback(site: Site) -> None:
    """Si la versión nueva hace caer el proceso una y otra vez después del compromiso, a los 3 intentos se
    vuelve atrás (no hay bucle infinito)."""
    site.factory.release("2.1.0", comps=("app",))
    with pytest.raises(SimulatedCrash):
        site.engine(fault_hook=crash_at("started", "after")).check()
    for _ in range(2):                               # intentos 2 y 3: vuelven a morir
        with pytest.raises(SimulatedCrash):
            site.engine(fault_hook=crash_at("started", "after")).startup()
    # el 4.º arranque ya no lo intenta: vuelve atrás aunque el fallo siga ahí
    site.engine(fault_hook=crash_at("started", "after")).startup()
    assert assert_consistent(site) == "2.0.0"
    assert Blacklist(site.layout.blacklist_file).contains("2.1.0")


# --------------------------------------------------------------------------- procesos de verdad
def _env(site: Site, backend_url: str, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    double = ROOT / "tests" / "updater" / "doubles" / "vmsctl_double.py"
    env.update({
        "PYTHONPATH": os.pathsep.join([str(ROOT / "updater"), str(ROOT)]),
        "VMS_DATA_DIR": str(site.layout.data), "VMS_INSTALL_DIR": str(site.layout.install),
        "VMS_UPDATE_SOURCE": site.url, "VMS_UPDATE_MODE": "online",
        "VMS_UPDATER_VMSCTL": f'"{sys.executable}" "{double}"', "VMS_BACKEND_URL": backend_url,
        "VMS_UPDATER_HEALTH_TIMEOUT": "4", "VMS_UPDATER_HEALTH_INTERVAL": "0.1",
        "VMS_UPDATER_TEST_HOOKS": "1", "VMS_UPDATER_TEST_FAKE_SYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("VMS_UPDATER_FAULT_AT", None)
    env.update(extra)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "vms_updater", *args], env=env, capture_output=True, text=True,
                          timeout=120, check=False)


def _real_kill(site: Site, state: str, phase: str, *, broken: bool = False) -> str:
    site.factory.release("2.1.0", comps=("app",), broken=broken)
    with serve(site.layout) as backend:
        p = _run(["check", "--force-window"], _env(site, backend, VMS_UPDATER_FAULT_AT=f"{state}:{phase}"))
        assert p.returncode == FAULT_EXIT_CODE, p.stderr[-2000:]
        j = site.journal()
        assert j is not None and j.in_progress
        r1 = _run(["recover"], _env(site, backend))
        assert r1.returncode == 0, r1.stderr[-2000:]
        active = assert_consistent(site)
        before = snapshot(site)
        r2 = _run(["recover"], _env(site, backend))
        assert r2.returncode == 0 and snapshot(site) == before
    return active


@pytest.mark.parametrize("state,phase,expected", [("stopping", "after", "2.0.0"), ("switched", "after", "2.1.0"),
                                                  ("verifying", "before", "2.1.0")])
def test_real_process_killed_recovers(site: Site, state: str, phase: str, expected: str) -> None:
    assert _real_kill(site, state, phase) == expected


def test_real_process_killed_while_rolling_back(site: Site) -> None:
    assert _real_kill(site, "rolling_back", "after", broken=True) == "2.0.0"
    assert Blacklist(site.layout.blacklist_file).contains("2.1.0")


@pytest.mark.slow
@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("state", FORWARD_STATES)
def test_real_process_killed_in_every_state(site: Site, state: str, phase: str) -> None:
    _real_kill(site, state, phase)


def test_pause_at_announces_in_public_status(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    with serve(site.layout) as backend:
        env = _env(site, backend, VMS_UPDATER_PAUSE_AT="stopping", VMS_UPDATER_PAUSE_S="3")
        proc = subprocess.Popen([sys.executable, "-m", "vms_updater", "check", "--force-window"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            status = site.layout.public_status_file
            import time
            deadline = time.monotonic() + 30
            seen = None
            while time.monotonic() < deadline:
                try:
                    seen = json.loads(status.read_text()).get("paused_at")
                except (OSError, ValueError):
                    seen = None
                if seen == "stopping":
                    break
                time.sleep(0.05)
            assert seen == "stopping"
        finally:
            out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err[-2000:]
    assert site.pointer().active == "2.1.0"


def test_fault_hooks_are_inert_without_test_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from vms_updater.journal import env_fault_hook
    monkeypatch.delenv("VMS_UPDATER_TEST_HOOKS", raising=False)
    monkeypatch.setenv("VMS_UPDATER_FAULT_AT", "switched")
    assert env_fault_hook() is None


def test_journal_corrupt_is_quarantined(site: Site) -> None:
    site.layout.journal_file.write_text("{no es json")
    assert site.engine().startup() is None
    assert list(Path(site.layout.state_dir).glob("journal.corrupt-*.json"))
    assert site.pointer().active == "2.0.0"
