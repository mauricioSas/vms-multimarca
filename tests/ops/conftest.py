"""Fixtures de las pruebas de B6: reutiliza el arnés de la API (app real con FakeEngine) y añade ayudas para
crear grabaciones de prueba con el nombre exacto de MediaMTX."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.api.conftest import ADMIN_PW, HEADERS, Harness, api, live_server, new_device  # noqa: F401 - fixtures
from tests.ops.stub_plugin import install as _install_stub_routes

# Las páginas de la v2 piden rutas de B6: el backend de pruebas de tests/web las necesita (ver stub_plugin.py).
_install_stub_routes()

__all__ = ["ADMIN_PW", "HEADERS", "Harness", "api", "live_server", "new_device", "write_segments", "wait_for"]


def write_segments(recordings_dir: Path, camera_id: str, start: datetime, count: int, *, seconds: int = 60,
                   size: int = 4096, seed: int = 1) -> list[Path]:
    """Segmentos fMP4 de prueba (contenido pseudoaleatorio, no vídeo real) con el nombre de MediaMTX."""
    folder = Path(recordings_dir) / camera_id / "main"
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(count):
        t = start + timedelta(seconds=seconds * i)
        name = t.strftime("%Y-%m-%d_%H-%M-%S") + f"-{t.microsecond:06d}+0000.mp4"
        p = folder / name
        p.write_bytes(bytes((seed * 31 + i * 7 + k) % 251 for k in range(size)))
        out.append(p)
    return out


async def wait_for(fn: Any, timeout: float = 15.0, interval: float = 0.05) -> Any:
    """Espera a que `fn()` (asíncrona) devuelva algo verdadero."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await fn()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("La condición no se cumplió a tiempo")
        await asyncio.sleep(interval)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    """Chromium de Playwright por módulo (como tests/web): con el API síncrono, solo en módulos sin pruebas async."""
    import os
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright no está instalado")
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"Chromium de Playwright no disponible: {exc}")
    try:
        yield b
    finally:
        b.close()
        pw.stop()
