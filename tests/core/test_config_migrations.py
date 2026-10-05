"""Migraciones de config.json (PLAN-V2 §2.8 y §4.4, CONTRATO §13.8). Dueño: B4.

- v1 → v2 igual a las fixtures esperadas (instalaciones de la v1 anonimizadas).
- Cadena v1 → v2 → v3 con una v3 simulada (la cadena es genérica: la próxima migración solo añade su paso).
- Versión mayor que la propia: solo lectura y rechazo de guardado (409 `config_read_only`).
- `python -m vms.core.config_migrations migrate --data-dir` (lo que ejecuta el actualizador con la versión
  nueva): atómico e idempotente.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vms.core import config_migrations as cm
from vms.core.config_store import ConfigRepository, ConfigStore
from vms.core.models import CONFIG_VERSION, AppConfig

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURES = sorted(p for p in FIX.glob("config_v1_*.json") if ".expected" not in p.name)


def load(p: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def test_there_are_v1_fixtures() -> None:
    assert len(V1_FIXTURES) >= 2


@pytest.mark.parametrize("fixture", V1_FIXTURES, ids=lambda p: p.stem)
def test_v1_to_v2_equals_expected_fixture(fixture: Path) -> None:
    doc, applied = cm.migrate(load(fixture))
    assert applied == [1]
    assert doc == load(fixture.with_name(fixture.stem + ".expected_v2.json"))


@pytest.mark.parametrize("fixture", V1_FIXTURES, ids=lambda p: p.stem)
def test_v1_fixture_loads_saves_and_reloads_cleanly(fixture: Path, tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(fixture.read_bytes())
    store = ConfigStore(path)
    cfg, warning = store.load()
    assert cfg.version == CONFIG_VERSION and warning is None and not store.read_only
    store.save(cfg)
    cfg2, warning2 = ConfigStore(path).load()
    assert warning2 is None and cfg2.model_dump() == cfg.model_dump()


def test_migrations_are_pure() -> None:
    src = load(V1_FIXTURES[0])
    snapshot = json.dumps(src, sort_keys=True)
    cm.migrate(src)
    assert json.dumps(src, sort_keys=True) == snapshot


def v2_to_v3_example(doc: dict[str, Any]) -> dict[str, Any]:
    """Ejemplo de paso futuro: renombra settings.retention.days → settings.retention.keep_days."""
    out = json.loads(json.dumps(doc))
    ret = out.get("settings", {}).get("retention", {})
    if "days" in ret:
        ret["keep_days"] = ret.pop("days")
    out["version"] = 3
    return out


def test_chain_v1_v2_v3() -> None:
    chain = {1: cm.v1_to_v2, 2: v2_to_v3_example}
    doc, applied = cm.migrate(load(FIX / "config_v1_tienda.json"), target=3, migrations=chain)
    assert applied == [1, 2] and doc["version"] == 3
    assert doc["settings"]["retention"] == {"keep_days": 30, "disk_guard_percent": 90}
    expected = load(FIX / "config_v1_tienda.expected_v2.json")
    expected["version"] = 3
    expected["settings"]["retention"] = {"keep_days": 30, "disk_guard_percent": 90}
    assert doc == expected
    # desde la v2 solo aplica el último paso
    doc2, applied2 = cm.migrate(load(FIX / "config_v1_tienda.expected_v2.json"), target=3, migrations=chain)
    assert applied2 == [2] and doc2 == expected


def test_chain_errors() -> None:
    with pytest.raises(ValueError, match="No hay migración"):
        cm.migrate({"version": 1}, target=3, migrations={1: cm.v1_to_v2})
    with pytest.raises(ValueError, match="no dejó la versión"):
        cm.migrate({"version": 1}, target=2, migrations={1: lambda d: {**d, "version": 1}})


def test_newer_config_is_read_only_and_never_saved(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    newer = load(FIX / "config_v1_tienda.expected_v2.json")
    newer["version"] = CONFIG_VERSION + 1
    newer["campo_de_la_v3"] = {"x": 1}
    path.write_text(json.dumps(newer), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    store = ConfigStore(path)
    cfg, warning = store.load()
    assert store.read_only and warning and "solo lectura" in warning
    with pytest.raises(cm.NewerConfigError) as exc:
        store.save(cfg)
    assert exc.value.status == 409 and exc.value.code == "config_read_only"
    repo = ConfigRepository(store)
    with pytest.raises(cm.NewerConfigError):
        asyncio.run(repo.update(lambda c: None))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest       # intacto
    assert repo.load_warning and "solo lectura" in repo.load_warning


def test_saving_a_config_with_newer_version_is_refused(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    with pytest.raises(cm.NewerConfigError):
        store.save(AppConfig(version=CONFIG_VERSION + 1))
    assert not (tmp_path / "config.json").exists()


def _cli(data_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "vms.core.config_migrations", "migrate", "--data-dir", str(data_dir)],
                          capture_output=True, text=True, env=env, timeout=60, check=False)


def test_cli_migrates_atomically_and_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "config" / "config.json"
    cfg.parent.mkdir()
    cfg.write_bytes((FIX / "config_v1_tienda.json").read_bytes())
    p = _cli(tmp_path)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["applied"] == [1]
    assert load(cfg) == load(FIX / "config_v1_tienda.expected_v2.json")
    before = cfg.read_bytes()
    p2 = _cli(tmp_path)
    assert p2.returncode == 0 and json.loads(p2.stdout)["applied"] == [] and cfg.read_bytes() == before
    assert not list(cfg.parent.glob("*.tmp-*"))


def test_cli_refuses_newer_and_tolerates_missing(tmp_path: Path) -> None:
    assert _cli(tmp_path).returncode == 0                               # sin config.json: nada que hacer
    cfg = tmp_path / "config" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"version": CONFIG_VERSION + 1}))
    before = cfg.read_bytes()
    p = _cli(tmp_path)
    assert p.returncode == 3 and "solo lectura" in p.stderr and cfg.read_bytes() == before
