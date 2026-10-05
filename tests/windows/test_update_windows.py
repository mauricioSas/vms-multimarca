"""e2e de Windows, pasos 6-9 de PLAN-V2 §4.6 (actualizaciones). Dueño: B4 (el arnés y los pasos 1-5 y 10-12 son de B3).

Se ejecuta solo dentro del flujo e2e de Windows (`VMS_WINDOWS_E2E=1`), con el Setup 2.0.0 de CI ya instalado por
los pasos 1-5 de B3 y estas variables del arnés:

| Variable | Qué es |
|---|---|
| `VMS_E2E_UPDATE_REPO` | carpeta `online/` del repositorio TUF de prueba que genera `build.yml` con `tools.release` (2.0.1 = cambio de app, 2.0.2 = cambio de engine, 2.0.3 = backend que no arranca) firmado con claves `dev` de CI |
| `VMS_E2E_PYTHON` | `python.exe` de la ranura activa del actualizador (para hablar con la tubería) |
| `VMS_E2E_CAMERA` | id de una cámara del simulador que graba |
| `VMS_E2E_ADMIN_PASSWORD` | contraseña del administrador creado en la instalación |

**Estado:** escrito contra el contrato (CONTRATO §15) y el arnés previsto; **no ejecutado** todavía: el arnés de
B3 y los binarios de B1 (`vmshost.exe`, `vmsctl.exe`) no existen aún en esta rama.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(os.environ.get("VMS_WINDOWS_E2E") != "1",
                                 reason="solo en el e2e de Windows (VMS_WINDOWS_E2E=1, arnés de B3)")]

DATA = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "VMSMultimarca"
BACKEND = "http://127.0.0.1:8600"


def pipe(req: dict[str, Any], timeout: float = 900) -> dict[str, Any]:
    p = subprocess.run([os.environ["VMS_E2E_PYTHON"], "-m", "vms_updater", "pipe", json.dumps(req)],
                       capture_output=True, text=True, timeout=timeout, check=False)
    return json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {"ok": False, "stderr": p.stderr}


def status() -> dict[str, Any]:
    return json.loads((DATA / "updater" / "public-status.json").read_text(encoding="utf-8"))


def pointer() -> dict[str, Any]:
    return json.loads((DATA / "state" / "active.json").read_text(encoding="utf-8"))


def max_gap_s(spans: list[dict[str, Any]], since: float, until: float) -> float:
    """Hueco máximo entre tramos grabados en [since, until] (segundos)."""
    from datetime import datetime
    pts = sorted((datetime.fromisoformat(s["start"].replace("Z", "+00:00")).timestamp(),
                  datetime.fromisoformat(s["end"].replace("Z", "+00:00")).timestamp()) for s in spans)
    gap, last_end = 0.0, since
    for start, end in pts:
        if end < since or start > until:
            continue
        gap = max(gap, start - last_end)
        last_end = max(last_end, end)
    return max(gap, until - last_end)


def timeline(cam: str) -> list[dict[str, Any]]:
    import httpx
    with httpx.Client(base_url=BACKEND, headers={"X-Requested-With": "vms"}) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": os.environ["VMS_E2E_ADMIN_PASSWORD"]})
        r = c.get(f"/api/recordings/{cam}/timeline")
        r.raise_for_status()
        data: Any = r.json()
        return list(data.get("spans", data) if isinstance(data, dict) else data)


@pytest.fixture(scope="module")
def update_server() -> Any:
    from tools.release.verify import serve
    with serve(Path(os.environ["VMS_E2E_UPDATE_REPO"])) as url:
        yield url


def _update_to(version: str) -> tuple[float, float]:
    t0 = time.time()
    r = pipe({"cmd": "check", "force_window": True})
    t1 = time.time()
    assert r.get("found") == version, r
    return t0, t1


def test_step6_app_update_without_recording_gap(update_server: str) -> None:
    t0, t1 = _update_to("2.0.1")
    assert status()["last_result"] == "update_ok" and pointer()["active"] == "2.0.1"
    assert max_gap_s(timeline(os.environ["VMS_E2E_CAMERA"]), t0, t1) <= 1.0


def test_step6_normal_user_cannot_open_pipe() -> None:
    """Lo comprueba el arnés de B3 abriendo la tubería con un usuario sin privilegios (runas): acceso denegado."""
    pytest.skip("necesita el usuario no administrador que crea el arnés de B3")


def test_step7_engine_update_gap_under_15s(update_server: str) -> None:
    t0, t1 = _update_to("2.0.2")
    assert pointer()["active"] == "2.0.2"
    assert max_gap_s(timeline(os.environ["VMS_E2E_CAMERA"]), t0, t1) <= 15.0


def test_step8_broken_version_rolls_back_and_is_blacklisted(update_server: str) -> None:
    cfg = (DATA / "config" / "config.json").read_bytes()
    r = pipe({"cmd": "check", "force_window": True})
    assert r.get("result") == "update_failed", r
    assert pointer()["active"] == "2.0.2" and status()["last_result"] == "update_failed"
    assert (DATA / "config" / "config.json").read_bytes() == cfg
    assert "2.0.3" in json.loads((DATA / "updater" / "blacklist.json").read_text())["versions"]
    assert pipe({"cmd": "check", "force_window": True}).get("result") == "no_update"
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VMSMultimarca") as k:
        assert winreg.QueryValueEx(k, "InstalledVersion")[0] == "2.0.2"


@pytest.mark.parametrize("state", ["stopping", "switched", "verifying"])
def test_step9_fault_injected_in_service_ends_in_good_version(state: str) -> None:
    """Con el servicio: VMS_UPDATER_FAULT_AT en el entorno de VMSUpdater, reinicio del servicio y comprobación."""
    pytest.skip("el arnés de B3 fija el entorno del servicio; la lógica ya se prueba con procesos de verdad "
                "en tests/updater/test_journal_faults.py (también en windows-latest)")
