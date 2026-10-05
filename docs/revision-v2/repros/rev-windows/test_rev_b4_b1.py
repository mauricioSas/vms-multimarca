"""Revisión cruzada B4↔B1: el vmshost REAL (Rust) vuelve atrás la versión a prueba y el actualizador (Python)
no se entera. Se ejecuta con las fixtures de tests/updater (repositorio TUF real, equipo simulado)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from tests.updater.conftest import _session_keys, factory, keyring, repo_url, site, crash_at, Site  # noqa: F401
from vms_updater.journal import SimulatedCrash
from vms_updater.pointer import PointerStore
from vms_updater.state_files import Blacklist

VMSHOST = Path(__file__).resolve().parents[1] / "target" / "debug" / "vmshost"


def _real_vmshost_updater_tick(site: Site, seconds: float = 1.5) -> str:
    """Arranca el vmshost real como VMSUpdater (consola) unos instantes: hace sus «updater_duties»."""
    L = site.layout
    for v in ("2.0.0", "2.1.0"):              # en macOS vmshost busca versions/<v>/bin/vmsctl (sin .exe)
        (L.version_dir(v) / "bin" / "vmsctl").write_bytes(b"#!/bin/sh\n")
    p = subprocess.Popen([str(VMSHOST), "service", "--name", "VMSUpdater", "--foreground",
                          "--install-dir", str(L.install), "--data-dir", str(L.data)], stdin=subprocess.PIPE)
    time.sleep(seconds)
    p.stdin.close()
    p.wait(timeout=30)
    return (L.logs_dir / "vmshost-VMSUpdater.log").read_text(encoding="utf-8")


def test_corte_de_luz_largo_en_verifying_vmshost_vuelve_atras_y_el_actualizador_no_restaura(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    with __import__("pytest").raises(SimulatedCrash):
        site.engine(fault_hook=crash_at("verifying", "before")).check()     # corte de luz en `verifying`
    cfg_migrated = json.loads(site.config_bytes())
    assert cfg_migrated.get("migrated_by") == "2.1.0"                       # config ya migrada a la 2.1.0
    ptr = site.pointer()
    assert ptr.active == "2.1.0" and ptr.trial
    # El equipo estuvo apagado más de 30 min (corte en la ventana 01:00-03:00): al volver la luz, el vmshost de
    # VMSUpdater hace su primera vuelta ANTES de lanzar el actualizador Python y vuelve atrás por plazo.
    ps = PointerStore(site.layout.pointer_file)
    ps.write(ptr.model_copy(update={"trial_since_unix": int(time.time()) - 3600}))
    log = _real_vmshost_updater_tick(site)
    print(log)
    ptr = site.pointer()
    assert ptr.active == "2.0.0" and not ptr.trial                          # vmshost (Rust) volvió atrás
    # Ahora arranca el actualizador Python y «recupera» el diario
    out = site.engine().startup()
    print("startup:", out)
    j = site.journal()
    print("diario:", j.dump() if j else None)
    cfg_after = json.loads(site.config_bytes())
    print("config.json tras la recuperación:", cfg_after)
    bl = Blacklist(site.layout.blacklist_file)
    # Lo que pasa de verdad:
    assert out is not None and "antes de cambiar de versión" in out.message_es    # diagnóstico falso
    assert cfg_after.get("migrated_by") == "2.1.0"                                  # NO se restauró el respaldo
    assert not bl.contains("2.1.0")                                                 # NO va a la lista negra
    again = site.engine().check()
    print("siguiente ciclo:", again)
    assert again.result == "update_ok" or site.pointer().active == "2.1.0"          # se reinstala la misma
