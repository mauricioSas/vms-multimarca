"""Vídeo en vivo de verdad en Chromium con el motor de producción (MediaMtxEngine) y la API real:

  simulador RTSP → MediaMtxEngine (ICE en todas las interfaces, como en la tienda) → proxy WHEP
  del backend → página /wall/1 real → <video> reproduciendo (videoWidth > 0 y el tiempo avanza).

Usa la API asíncrona de Playwright (no deja un bucle de eventos registrado en el hilo principal).
"""
from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from tests.api.conftest import ADMIN_PW, HEADERS
from tools.camsim.simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice
from tools.mocks.hikvision import HikChannel, HikvisionMock
from vms.api import create_app
from vms.core.credentials import CredentialStore
from vms.core.settings import VmsSettings

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.needs_mediamtx, pytest.mark.needs_ffmpeg,
              pytest.mark.needs_browser]


@pytest.fixture
async def served(settings: VmsSettings, credential_store: CredentialStore, mediamtx_bin: str,
                 camsim_factory: Callable[..., CameraSimulator], mock_server: Callable[[Any], Any]
                 ) -> AsyncIterator[tuple[str, CameraSimulator]]:
    sim = camsim_factory([SimDevice("hik1", "hikvision", channels=1)])
    api_srv = mock_server(HikvisionMock(password=DEFAULT_PASSWORD, channels=[HikChannel("Entrada")]).app)
    s = settings.model_copy(update={"admin_initial_password": SecretStr(ADMIN_PW), "mediamtx_bin": Path(mediamtx_bin)})
    app = create_app(s, credential_store=credential_store, heartbeat=False, apply_delay=0.2)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on", log_config=None))
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        if task.done():
            task.result()
        await asyncio.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base, headers=HEADERS, timeout=30) as c:
            assert (await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})).status_code == 200
            r = await c.post("/api/devices", json={
                "name": "NVR", "vendor": "hikvision", "kind": "nvr", "host": "127.0.0.1", "http_port": api_srv.port,
                "rtsp_port": sim.device("hik1").port, "username": "admin", "password": DEFAULT_PASSWORD,
                "import_channels": "all"})
            assert r.status_code == 201, r.text
            cam = r.json()["cameras"][0]
            assert (await c.put("/api/walls/1", json={"grid": 4, "cells": [cam]})).status_code == 200
        yield base, sim
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)


async def test_live_wall_plays_webrtc_from_production_engine(served: tuple[str, CameraSimulator]) -> None:
    base, _ = served
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright no está instalado")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium de Playwright no disponible: {exc}")
        try:
            page = await (await browser.new_context(base_url=base)).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto("/login?next=/wall/1")
            await page.fill("#username", "admin")
            await page.fill("#password", ADMIN_PW)
            await page.click("#login-submit")
            await page.wait_for_url("**/wall/1", timeout=15000)
            await page.wait_for_function(
                "() => { const v = document.querySelector('video'); return v && v.videoWidth > 0 && v.currentTime > 0.5 }",
                timeout=30000)
            t1 = await page.evaluate("() => document.querySelector('video').currentTime")
            await asyncio.sleep(1.5)
            t2 = await page.evaluate("() => document.querySelector('video').currentTime")
            size = await page.evaluate("() => [document.querySelector('video').videoWidth, "
                                       "document.querySelector('video').videoHeight]")
            assert t2 > t1, "el vídeo en vivo debe avanzar"
            assert size == [320, 180], "con cuadrícula de 4 el muro usa el subflujo"
            assert not errors, errors
        finally:
            await browser.close()
