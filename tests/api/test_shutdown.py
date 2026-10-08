"""Parada ordenada del backend dentro del plazo de vmsctl (CONTRATO §14.1).

VMSBackend.log y vms.log del PC de la tienda, 06/10 15:21 (actualización de la beta.1 a la beta.2): uvicorn
esperó 10 s a que cerrara un panel con `/api/events` abierto («Waiting for connections to close») y vmsctl, que
también da 10 s, mató el backend antes de que llegara a pararse («no paró en 10 s: se fuerza»; nunca salió
«Deteniendo VMS Multimarca»). Un flujo SSE solo terminaba cuando el navegador colgaba.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from tests.api.conftest import ADMIN_PW, HEADERS, Harness
from tests.conftest import get_free_port
from tests.fakes import FakeEngine
from vms.api import app as app_mod, create_app, serve as serve_mod
from vms.core.credentials import CredentialStore
from vms.core.settings import VmsSettings

ROOT = Path(__file__).resolve().parents[2]

# Lo que no fija el código del backend: parar el motor en modo attach (cancelar la vigilancia y cerrar su
# cliente HTTP) y el proxy, y que el proceso salga después (hilos, registro).
ENGINE_AND_PROXY_S = 3
PROCESS_EXIT_S = 3


def catalog_grace_s(const: str) -> int:
    """`grace_s` de un servicio en el catálogo de Rust (`native/common/src/services.rs`)."""
    src = (ROOT / "native" / "common" / "src" / "services.rs").read_text(encoding="utf-8")
    m = re.search(rf"name:\s*{const},.*?grace_s:\s*(\d+)", src, re.S)
    assert m, f"no se encuentra grace_s de {const} en services.rs"
    return int(m.group(1))


def test_stop_budget_fits_in_vmsctl_grace() -> None:
    graceful = serve_mod.COMMON["timeout_graceful_shutdown"]
    budget = graceful + app_mod.STOP_FLUSH_APPLY_S + ENGINE_AND_PROXY_S + PROCESS_EXIT_S
    grace = catalog_grace_s("BACKEND")
    assert budget <= grace, (f"la parada del backend puede tardar {budget} s y vmsctl lo mata a los {grace} s: "
                             "sube grace_s de VMSBackend o baja las esperas")


async def test_open_event_stream_does_not_hold_the_stop(settings: VmsSettings, credential_store: CredentialStore,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    """Backend real (`serve`, con su lifespan) y un panel con `/api/events` abierto: la parada que pide vmsctl
    (cerrar la entrada estándar → SIGINT → `should_exit`) termina en poco más de un instante."""
    caplog.set_level(logging.INFO)
    port = get_free_port()
    s = settings.model_copy(update={"admin_initial_password": SecretStr(ADMIN_PW), "http_host": "127.0.0.1",
                                    "http_port": port})
    app = create_app(s, engine=FakeEngine(), credential_store=credential_store, heartbeat=False, apply_delay=0.01)
    servers: list[uvicorn.Server] = []

    class Recorded(uvicorn.Server):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            servers.append(self)

    monkeypatch.setattr(serve_mod.uvicorn, "Server", Recorded)
    served = asyncio.create_task(serve_mod.serve(app, s))
    try:
        for _ in range(500):
            if servers and servers[0].started:
                break
            assert not served.done(), "el backend no arrancó"
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=HEADERS, timeout=30) as c:
            r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
            assert r.status_code == 200, r.text
            async with c.stream("GET", "/api/events") as resp:
                assert resp.status_code == 200
                lines = resp.aiter_lines()
                while not (await asyncio.wait_for(anext(lines), 5)).startswith("event: status"):
                    pass

                async def drain() -> str:
                    try:
                        async for _ in lines:
                            pass
                    except httpx.HTTPError as exc:       # uvicorn cortó el flujo al agotar su plazo
                        return f"cortado: {exc!r}"
                    return "terminado"

                reader = asyncio.create_task(drain())
                t0 = time.monotonic()
                servers[0].should_exit = True            # lo que hace SIGINT (vmsctl cierra la entrada)
                assert await asyncio.wait_for(served, 30) is True
                elapsed = time.monotonic() - t0
                ended = await asyncio.wait_for(reader, 5)
    finally:
        if not served.done():
            servers[0].should_exit = True
            await asyncio.wait_for(served, 30)
    assert elapsed < 2.5, f"la parada tardó {elapsed:.1f} s con un panel abierto"
    assert "timeout graceful shutdown exceeded" not in caplog.text
    assert "Deteniendo VMS Multimarca" in caplog.text           # la parada de la app sí llega a hacerse
    assert ended == "terminado"                                 # sin cortes: EventSource vuelve a conectar


async def test_begin_stop_ends_open_event_streams_at_once(api: Harness, live_server: str) -> None:
    async with httpx.AsyncClient(base_url=live_server, headers=HEADERS, timeout=10) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        assert r.status_code == 200
        async with c.stream("GET", "/api/events") as resp:
            lines = resp.aiter_lines()
            while not (await asyncio.wait_for(anext(lines), 5)).startswith("event: status"):
                pass
            t0 = time.monotonic()
            api.state.begin_stop()

            async def drain() -> None:
                async for _ in lines:
                    pass

            await asyncio.wait_for(drain(), 5)
            assert time.monotonic() - t0 < 0.5      # el bus despierta al flujo: no espera a su próxima vuelta
    assert api.state.bus.subscribers == 0
