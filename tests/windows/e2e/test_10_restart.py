"""Paso 10 (PLAN-V2 §4.6): reinicio simulado (``Restart-Computer`` no es viable en el runner) y negativa a bajar
de versión, con la instalación del paso 2 todavía en el equipo."""
from __future__ import annotations

import subprocess
import time

import pytest

from tests.windows import harness as h
from tests.windows.e2e.common import calls, installed_vmsctl, new_secrets
from tests.windows.conftest import E2E

pytestmark = [pytest.mark.e2e]


def _vmsctl(e2e: E2E, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(installed_vmsctl(e2e)), *args, "--json"], capture_output=True, text=True,
                          timeout=180, env=e2e.env.environ(), check=False)


@pytest.mark.paso("10", "Reinicio simulado: vmsctl services stop + start; los servicios vuelven solos")
def test_step10_restart(e2e: E2E, step: h.Step) -> None:
    if not e2e.state.get("installed"):
        pytest.skip("no hay instalación (falló el paso 2)")
    started = time.monotonic()
    stop = _vmsctl(e2e, "services", "stop")
    start = _vmsctl(e2e, "services", "start")
    assert stop.returncode == 0 and start.returncode == 0, stop.stdout + start.stdout
    # El vmsctl real solo da por sano un proceso que lleva 20 s en marcha: el mismo plazo que el instalador.
    health = _vmsctl(e2e, "health", "wait", "--timeout", "10" if e2e.doubles else "120")
    assert health.returncode == 0, health.stdout
    step.details["segundos"] = round(time.monotonic() - started, 1)
    diag = e2e.artifacts / "diag-bundle.zip"
    bundle = _vmsctl(e2e, "diag", "bundle", "--out", str(diag))
    assert bundle.returncode == 0 and diag.is_file(), bundle.stdout
    if e2e.doubles:
        step.note("Dobles: se comprueba que las órdenes existen y responden. Matar mediamtx.exe y python.exe y "
                  "medir la vuelta en < 10 s necesita los servicios reales (B1).")
    else:
        # Solo los procesos del producto: «taskkill /IM python.exe» mataría también al pytest de este e2e.
        before = _product_pids()
        assert before, "no hay procesos del producto en marcha"
        prog = str(h.program_dir()).replace("'", "''")
        h.powershell(f"Get-Process mediamtx, python -ErrorAction SilentlyContinue | "
                     f"Where-Object {{ $_.Path -like '{prog}\\*' }} | Stop-Process -Force")
        # Vuelven solos en < 10 s (los relanza vmsctl run); «sano» además exige 20 s seguidos en marcha.
        assert h.wait_until(lambda: len(_product_pids() - before) >= len(before), 10, 0.5), (before, _product_pids())
        health = _vmsctl(e2e, "health", "wait", "--timeout", "120")
        assert health.returncode == 0, health.stdout


def _product_pids() -> set[int]:
    """PID de mediamtx.exe y python.exe que corren desde la carpeta del programa."""
    prog = str(h.program_dir()).replace("'", "''")
    out = h.powershell(f"Get-Process mediamtx, python -ErrorAction SilentlyContinue | "
                       f"Where-Object {{ $_.Path -like '{prog}\\*' }} | ForEach-Object {{ $_.Id }}")
    return {int(x) for x in out.split() if x.strip().isdigit()}


@pytest.mark.paso("10b", "Negativa a bajar de versión (CONTRATO §13.7) y /ALLOWDOWNGRADE")
def test_step10b_refuses_downgrade(e2e: E2E, step: h.Step) -> None:
    if not e2e.state.get("installed"):
        pytest.skip("no hay instalación (falló el paso 2)")
    h.reg_set(h.VMS_KEY, "InstalledVersion", "99.0.0")   # como si el actualizador ya hubiera subido a la 99
    before = len(calls(e2e))
    res = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", "/MINFREEGB=1"], "bajar-version")
    text = res.text()
    step.details["codigo"] = res.code
    assert res.code == 1, f"esperado 1 (no arranca), fue {res.code}"
    assert "99.0.0" in text and "más nueva" in text
    assert len(calls(e2e)) == before, "no debe tocar nada"
    secrets = new_secrets(e2e, "bajar-secrets.json")
    res2 = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", "/ALLOWDOWNGRADE", f"/SECRETS={secrets}",
                                                "/MINFREEGB=1"], "bajar-version-permitido")
    assert res2.code == 0, res2.text()[-4000:]
    assert "ALLOWDOWNGRADE" in res2.text()
    assert h.reg_get(h.VMS_KEY, "InstalledVersion") == e2e.version
