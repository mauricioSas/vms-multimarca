"""Campos extra del latido aportados por los bloques (vms/core/heartbeat_extras.py) y ajustes v2."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from vms.core import heartbeat_extras
from vms.core.heartbeat_extras import MAX_EXTRA_BYTES, collect_extras
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings

SELF = __name__


def ok_update(paths: AppPaths) -> dict[str, Any]:
    return {"installed": "2.1.0", "state": "good", "data_dir": paths.base.name}


def nothing(paths: AppPaths) -> None:
    return None


def boom(paths: AppPaths) -> dict[str, Any]:
    raise RuntimeError("roto")


def not_a_dict(paths: AppPaths) -> list[int]:
    return [1, 2]


def too_big(paths: AppPaths) -> dict[str, Any]:
    return {"problems": ["x" * (MAX_EXTRA_BYTES + 1)]}


def not_json(paths: AppPaths) -> dict[str, Any]:
    return {"nan": float("nan")}


def test_default_providers_are_declared_and_absent_today(tmp_path: Path) -> None:
    assert set(heartbeat_extras.PROVIDERS) == {"update", "health", "evidence_key"}
    # en la fase 0 ningún bloque ha entregado su proveedor: el latido sale sin esas claves
    assert collect_extras(AppPaths(tmp_path)) == {}


def test_only_valid_providers_contribute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(heartbeat_extras, "PROVIDERS", {
        "update": f"{SELF}:ok_update", "health": f"{SELF}:boom", "evidence_key": f"{SELF}:nothing",
        "a": f"{SELF}:not_a_dict", "b": f"{SELF}:too_big", "c": f"{SELF}:not_json",
        "d": f"{SELF}:no_existe", "e": "modulo_que_no_existe.heartbeat:f",
    })
    with caplog.at_level(logging.DEBUG, logger="vms.heartbeat.extras"):
        out = collect_extras(AppPaths(tmp_path / "datos"))
    assert out == {"update": {"installed": "2.1.0", "state": "good", "data_dir": "datos"}}
    text = caplog.text
    assert "falló" in text and "no devolvió un objeto" in text and "ocupa" in text and "no es JSON" in text
    assert "aún no disponible" in text   # módulo del bloque todavía sin entregar: solo debug


def test_broken_import_inside_a_provider_is_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    pkg = tmp_path / "prov_roto"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import dependencia_que_falta\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(heartbeat_extras, "PROVIDERS", {"update": "prov_roto:f"})
    with caplog.at_level(logging.WARNING, logger="vms.heartbeat.extras"):
        assert collect_extras(AppPaths(tmp_path)) == {}
    assert "No se pudo importar" in caplog.text


async def test_direct_heartbeat_includes_extras(settings: VmsSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    from vms.api.app import create_app

    monkeypatch.setattr(heartbeat_extras, "PROVIDERS", {"update": f"{SELF}:ok_update"})
    app = create_app(settings)
    payload = await app.state.vms.heartbeat_payload()
    assert payload["update"]["installed"] == "2.1.0" and payload["status"] in ("ok", "degraded", "down")


def test_v2_settings_defaults_and_validation() -> None:
    s = VmsSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.engine_mode == "child" and s.update_source == ""
    for ok in ("https://updates.example.com/", "http://127.0.0.1:8080/", "file:///D:/vms-updates"):
        assert VmsSettings(_env_file=None, update_source=ok).update_source == ok  # type: ignore[call-arg]
    for bad in ("ftp://x/", "D:\\vms-updates", "updates.example.com"):
        with pytest.raises(ValueError, match="VMS_UPDATE_SOURCE"):
            VmsSettings(_env_file=None, update_source=bad)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        VmsSettings(_env_file=None, engine_mode="servicio")  # type: ignore[call-arg]


async def test_agent_heartbeat_includes_extras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from central.agent import HeartbeatAgent
    from tests.central.test_heartbeat import agent_settings, backend_transport

    monkeypatch.setattr(heartbeat_extras, "PROVIDERS", {"update": f"{SELF}:ok_update", "health": f"{SELF}:boom"})
    agent = HeartbeatAgent(agent_settings(tmp_path), backend_transport=backend_transport())
    try:
        _site, payload = await agent.collect()
    finally:
        await agent.aclose()
    assert payload.model_extra and payload.model_extra["update"]["installed"] == "2.1.0"
    assert "health" not in payload.model_extra
