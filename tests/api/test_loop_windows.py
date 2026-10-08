"""Las pruebas del backend siguen en Proactor en Windows, como el backend real (tests/conftest.py).

El backend necesita Proactor para lanzar MediaMTX como subproceso, así que psycopg asíncrono no funciona dentro de
él (vms/core/aio.py). Si sus pruebas corrieran en un bucle selector, esconderían ese fallo.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import ROOT, needs_selector_loop
from vms.core import aio


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor solo existe en Windows")
async def test_backend_tests_keep_proactor() -> None:
    assert not aio.psycopg_async_supported()


def test_selector_loop_rule() -> None:
    tests = ROOT / "tests"
    assert needs_selector_loop(tests / "central" / "test_x.py", [])
    assert needs_selector_loop(tests / "analytics" / "test_x.py", [])
    assert needs_selector_loop(tests / "db" / "test_x.py", [])
    assert needs_selector_loop(tests / "ops" / "test_central_and_counts.py", ["admin_client", "central_app"])
    assert not needs_selector_loop(Path(__file__), ["settings", "pg_dsn"])
    assert not needs_selector_loop(tests / "core" / "test_x.py", [])
