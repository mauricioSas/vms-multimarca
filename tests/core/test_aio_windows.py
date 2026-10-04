"""Regresión: en Windows, psycopg asíncrono no funciona con el bucle Proactor (el de asyncio.run y
uvicorn por defecto). Los procesos que usan PostgreSQL asíncrono arrancan con SelectorEventLoop y
el latido del backend (que sí necesita Proactor para lanzar MediaMTX) usa psycopg síncrono.

En macOS se emula Windows: `sys.platform = "win32"` y `asyncio.ProactorEventLoop` = una subclase
del bucle selector, que es la condición exacta que psycopg comprueba.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

import psycopg
import pytest
import uvicorn

from analytics.storage import PgStore, StoreError
from central.heartbeat import HeartbeatSender
from vms.core import aio
from vms.core.models import Site


class FakeProactor(asyncio.SelectorEventLoop):
    """Hace de ProactorEventLoop de Windows (psycopg lo rechaza por su clase)."""


@pytest.fixture
def emulate_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "ProactorEventLoop", FakeProactor, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")


LINE = {"type": "line", "rule_id": "rule-door0001", "camera_id": "cam-door0001",
        "minute": "2026-10-04T10:00:00+00:00", "count_in": 3, "count_out": 1}


def test_loop_factory_is_selector_on_windows(emulate_windows: None) -> None:
    assert aio.loop_factory() is asyncio.SelectorEventLoop
    assert aio.uvicorn_loop() == "asyncio:SelectorEventLoop"
    cfg = uvicorn.Config(app=lambda *a: None, loop=aio.uvicorn_loop())
    assert cfg.get_loop_factory() is asyncio.SelectorEventLoop


def test_loop_factory_default_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert aio.loop_factory() is None and aio.uvicorn_loop() == "auto"


def test_proactor_breaks_psycopg_and_selector_fixes_it(emulate_windows: None, pg_dsn: str) -> None:
    store_args = (pg_dsn, "site-win-001")

    async def write() -> None:
        store = PgStore(*store_args)
        try:
            await store.write([LINE])
        finally:
            await store.close()

    # 1) Lo que pasaba antes: con el bucle por defecto de Windows, cada lote fallaba.
    with pytest.raises(StoreError, match="ProactorEventLoop"):
        asyncio.run(write(), loop_factory=FakeProactor)
    # 2) Con el bucle que eligen ahora los procesos (vms.core.aio.run) se escribe.
    aio.run(write())
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT count_in FROM line_counts_minute WHERE site_id='site-win-001'").fetchone() == (3,)


def test_heartbeat_sender_works_inside_proactor_loop(emulate_windows: None, pg_dsn: str) -> None:
    """El backend corre en Proactor (lo necesita para MediaMTX): el latido debe funcionar igual."""
    async def collect() -> dict[str, Any]:
        return {"status": "ok", "version": "t", "cameras": [{"camera_id": "cam-door0001", "name": "Puerta"}]}

    async def beat() -> bool:
        assert isinstance(asyncio.get_running_loop(), FakeProactor)
        hb = HeartbeatSender(pg_dsn, Site(id="site-win-002", name="Tienda Win", timezone="Europe/Madrid"), 60,
                             collect)
        ok = await hb.send_once()
        assert hb.last_error == ""
        return ok

    assert asyncio.run(beat(), loop_factory=FakeProactor) is True
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT name FROM sites WHERE site_id='site-win-002'").fetchone() == ("Tienda Win",)
        assert c.execute("SELECT count(*) FROM site_cameras WHERE site_id='site-win-002'").fetchone() == (1,)


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    def fake_run(coro: Coroutine[Any, Any, Any], *, loop_factory: Any = None) -> int:
        seen.append(loop_factory)
        coro.close()
        return 0

    monkeypatch.setattr(aio.asyncio, "run", fake_run)
    return seen


def test_entry_points_use_selector_loop_on_windows(emulate_windows: None, monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Any) -> None:
    monkeypatch.setenv("VMS_DATA_DIR", str(tmp_path / "datos"))
    seen = _capture_run(monkeypatch)
    import analytics.__main__ as amain
    import analytics.reports.__main__ as rmain

    assert amain.main(["check"]) == 0
    assert rmain.main(["--site", "site-x-001", "--dsn", "postgresql://x@127.0.0.1:1/x", "--no-llm"]) == 0
    assert seen == [asyncio.SelectorEventLoop, asyncio.SelectorEventLoop]

    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    import central.__main__ as cmain
    from central.settings import CentralSettings
    cmain._serve(CentralSettings(_env_file=None, data_dir=tmp_path / "central"))  # type: ignore[call-arg]
    assert captured["loop"] == "asyncio:SelectorEventLoop"
