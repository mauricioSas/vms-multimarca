"""Configuración del e2e de Windows (``e2e/`` de B3 y ``test_update_*.py`` de B4, pasos 6-9).

Solo corre en Windows con un instalador (``python -m tests.windows.run_e2e``). Las pruebas portables de esta
carpeta (``test_layout.py``, ``test_build_tools.py``…) no usan nada de aquí.

Variables (prefijo ``VMS_TEST_``: ``tests/conftest.py`` no las borra):
  VMS_TEST_WIN_INSTALLER   ruta del Setup .exe (obligatoria)
  VMS_TEST_WIN_WORK        carpeta de trabajo (por defecto .tmp/e2e-windows)
  VMS_TEST_WIN_ARTIFACTS   carpeta de artefactos (por defecto <trabajo>/artefactos)
  VMS_TEST_WIN_MODE        doubles | real | auto (auto: lee build-info.json junto al instalador)
  VMS_TEST_WIN_V1          1 = ejecutar también la actualización desde una v1 de install.ps1 (descarga ~200 MB)
  VMS_TEST_WIN_MANIFEST    payload-manifest.json de la build (por defecto, el de build-info.json)

Los pasos comparten estado y van en orden: ``run_e2e`` desactiva el orden aleatorio (``-p no:randomly``).
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.windows.harness import Env, Results, Step

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = os.environ.get("VMS_TEST_WIN_INSTALLER", "")
READY = sys.platform == "win32" and bool(INSTALLER) and Path(INSTALLER).is_file()
SKIP_REASON = "e2e de Windows: solo en Windows con VMS_TEST_WIN_INSTALLER (python -m tests.windows.run_e2e)"


@dataclass
class E2E:
    installer: Path
    work: Path
    artifacts: Path
    mode: str
    version: str
    manifest: Path | None
    env: Env
    results: Results
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def doubles(self) -> bool:
        return self.mode == "doubles"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "paso(key, title): paso del e2e de Windows (PLAN-V2 §4.6)")


def is_e2e_file(path: Path) -> bool:
    """Pruebas que instalan de verdad: ``e2e/test_*.py`` (B3) y ``test_update_*.py`` (B4)."""
    here = Path(__file__).parent
    return path.parent == here / "e2e" or (path.parent == here and path.name.startswith("test_update_"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if is_e2e_file(Path(str(item.fspath))):
            item.add_marker(pytest.mark.e2e)
            if not READY:
                item.add_marker(pytest.mark.skip(reason=SKIP_REASON))


def _build_info(installer: Path) -> dict[str, Any]:
    f = installer.parent / "build-info.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


_SESSION: dict[str, E2E] = {}


@pytest.fixture(scope="session")
def e2e() -> E2E:
    if "e2e" in _SESSION:
        return _SESSION["e2e"]
    installer = Path(INSTALLER).resolve()
    info = _build_info(installer)
    work = Path(os.environ.get("VMS_TEST_WIN_WORK") or ROOT / ".tmp" / "e2e-windows").resolve()
    work.mkdir(parents=True, exist_ok=True)
    artifacts = Path(os.environ.get("VMS_TEST_WIN_ARTIFACTS") or work / "artefactos").resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    mode = os.environ.get("VMS_TEST_WIN_MODE", "auto")
    if mode == "auto":
        mode = "doubles" if info.get("doubles") else "real"
    manifest_env = os.environ.get("VMS_TEST_WIN_MANIFEST")
    manifest = Path(manifest_env) if manifest_env else None
    if manifest is None and info.get("payload"):
        candidate = Path(str(info["payload"])).parent / "payload-manifest.json"
        manifest = candidate if candidate.is_file() else None
    version = str(info.get("version") or installer.stem.replace("VMSMultimarca-Setup-", ""))
    # Registros dentro de los artefactos: si el e2e se corta (timeout), lo ya escrito se sube igual.
    env = Env(work, artifacts / "vmsctl-calls.jsonl", logs=artifacts / "registros")
    obj = E2E(installer, work, artifacts, mode, version, manifest, env,
              Results(installer=str(installer), mode=mode, version=version))
    _SESSION["e2e"] = obj
    return obj


@pytest.fixture
def step(request: pytest.FixtureRequest, e2e: E2E) -> Step:
    marker = request.node.get_closest_marker("paso")
    key, title = (marker.args if marker else (request.node.name, request.node.name))
    s = Step(key=str(key), title=str(title))
    e2e.results.steps.append(s)
    request.node._vms_step = s
    request.node._vms_started = time.monotonic()
    return s


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    outcome = yield
    report = outcome.get_result()
    s: Step | None = getattr(item, "_vms_step", None)
    if s is None:
        return
    if report.when == "setup" and report.skipped:
        s.ok = None
        s.note(f"omitido: {report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr}")
    elif report.when == "call":
        s.seconds = round(time.monotonic() - getattr(item, "_vms_started", time.monotonic()), 1)
        if report.skipped:
            s.ok = None
            reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
            s.note(f"omitido: {reason}")
        else:
            s.ok = report.passed
            if report.failed:
                text = str(report.longrepr)
                s.note("FALLO: " + text[-3000:])
                # En el momento, no al final: si un paso posterior se cuelga, el motivo ya está en el registro de CI.
                print(f"\n----- FALLO en {item.nodeid} -----\n{text[-6000:]}\n-----", file=sys.__stdout__, flush=True)
    if report.when in ("call", "teardown"):
        _save()


def _save() -> None:
    obj = _SESSION.get("e2e")
    if obj is not None:
        obj.results.save(obj.artifacts / "results-windows.json")
        (obj.artifacts / "RESULTADOS-windows.md").write_text(obj.results.markdown(), encoding="utf-8")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _save()
