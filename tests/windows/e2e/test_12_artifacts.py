"""Paso 12 (PLAN-V2 §4.6): artefactos de la ejecución. ``results-windows.json`` y ``RESULTADOS-windows.md`` los
escribe ``conftest.py`` al terminar la sesión; aquí se reúnen registros, llamadas a vmsctl y capturas, y se deja
el equipo limpio."""
from __future__ import annotations

import shutil

import pytest

from tests.windows import harness as h
from tests.windows.conftest import E2E

pytestmark = [pytest.mark.e2e]


@pytest.mark.paso("12", "Artefactos: registros de instalación, llamadas a vmsctl, informe de diagnóstico y capturas")
def test_step12_artifacts(e2e: E2E, step: h.Step) -> None:
    logs = e2e.artifacts / "registros"
    if e2e.env.logs.is_dir():
        shutil.copytree(e2e.env.logs, logs, dirs_exist_ok=True)
    if e2e.env.calls_log.is_file():
        shutil.copy2(e2e.env.calls_log, e2e.artifacts / "vmsctl-calls.jsonl")
    step.details["limpieza"] = h.force_clean(e2e.env)
    present = sorted(p.relative_to(e2e.artifacts).as_posix() for p in e2e.artifacts.rglob("*") if p.is_file())
    step.details["archivos"] = len(present)
    assert any(p.startswith("registros/") for p in present), "faltan los registros de instalación"
    assert "vmsctl-calls.jsonl" in present or not e2e.doubles
    for log in logs.glob("*.log"):
        text = h.read_text_any(log)
        for secret in ("Clave'e2e", "tok-e2e-0123456789"):
            assert secret not in text, f"secreto en {log.name}"
