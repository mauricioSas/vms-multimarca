"""Arnés de pruebas de la API: app real con FakeEngine y fabricantes simulados (sin red ni MediaMTX)."""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from tests.fakes import FakeDeviceClient, FakeEngine
from vms.api import create_app
from vms.core.credentials import CredentialStore
from vms.core.interfaces import DeviceTestResult, DiscoveredDevice
from vms.core.models import Device, DeviceBase
from vms.core.settings import VmsSettings

ADMIN_PW = "Admin#12345"
HEADERS = {"X-Requested-With": "vms"}


@dataclass
class Harness:
    app: FastAPI
    engine: FakeEngine
    creds: CredentialStore
    settings: VmsSettings
    behavior: dict[str, Any] = field(default_factory=lambda: {"reachable": True, "password_ok": True, "channels": 4})
    client_calls: list[tuple[str, str]] = field(default_factory=list)   # (device_id, password)
    tester_calls: list[tuple[str, str]] = field(default_factory=list)   # (host, password)
    discovered: list[DiscoveredDevice] = field(default_factory=list)
    _clients: list[httpx.AsyncClient] = field(default_factory=list)

    @property
    def state(self) -> Any:
        return self.app.state.vms

    def client(self, *, ip: str = "127.0.0.1", csrf: bool = True) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(ip, 50000))
        c = httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=HEADERS if csrf else None)
        self._clients.append(c)
        return c

    async def login(self, username: str = "admin", password: str = ADMIN_PW, *, ip: str = "127.0.0.1") -> httpx.AsyncClient:
        c = self.client(ip=ip)
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return c

    async def operator(self) -> httpx.AsyncClient:
        admin = await self.login()
        if self.state.users.get("operador") is None:
            r = await admin.post("/api/users", json={"username": "operador", "password": "Operador#1", "role": "operator"})
            assert r.status_code == 201, r.text
        return await self.login("operador", "Operador#1")

    async def kiosk(self) -> httpx.AsyncClient:
        c = self.client()
        r = await c.get("/api/auth/kiosk", params={"token": "token-kiosco-de-pruebas", "next": "/wall/2"})
        assert r.status_code == 303, r.text
        return c

    async def settle(self) -> None:
        """Espera a que el motor reciba la configuración (antirrebote)."""
        await asyncio.sleep(0.05)
        await self.state.flush_apply()

    async def close(self) -> None:
        for c in self._clients:
            await c.aclose()


@pytest.fixture
async def api(settings: VmsSettings, credential_store: CredentialStore) -> AsyncIterator[Harness]:
    engine = FakeEngine()
    s = settings.model_copy(update={"admin_initial_password": SecretStr(ADMIN_PW)})
    holder: dict[str, Harness] = {}

    def factory(device: Device, password: str) -> FakeDeviceClient:
        h = holder["h"]
        h.client_calls.append((device.id, password))
        b = h.behavior
        return FakeDeviceClient(device.vendor, channels=b["channels"], password_ok=b["password_ok"],
                                reachable=b["reachable"])

    async def tester(device: DeviceBase, password: str) -> DeviceTestResult:
        holder["h"].tester_calls.append((device.host, password))
        ok = password == "buena"
        return DeviceTestResult(ok=ok, reachable=True, auth_ok=ok, message="ok" if ok else "Usuario o contraseña incorrectos")

    async def discoverer(timeout: float) -> list[DiscoveredDevice]:
        return list(holder["h"].discovered)

    app = create_app(s, engine=engine, credential_store=credential_store, client_factory=factory,
                     device_tester=tester, discoverer=discoverer, heartbeat=False, apply_delay=0.01)
    h = Harness(app, engine, credential_store, s)
    holder["h"] = h
    async with app.router.lifespan_context(app):
        yield h
        await h.close()


@pytest.fixture
async def live_server(api: Harness) -> AsyncIterator[str]:
    """Sirve la app del arnés en un puerto real, en el mismo bucle (para SSE y streaming)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, lifespan="off",
                                           log_config=None, access_log=False))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def new_device(c: httpx.AsyncClient, **over: Any) -> dict[str, Any]:
    body = {"name": "NVR Tienda", "vendor": "hikvision", "kind": "nvr", "host": "10.0.0.5", "username": "admin",
            "password": "Cl@ve#1", **over}
    r = await c.post("/api/devices", json=body)
    assert r.status_code == 201, r.text
    return r.json()
