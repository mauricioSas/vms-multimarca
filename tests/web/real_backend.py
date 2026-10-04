"""Adaptador para ejecutar las pruebas de la interfaz contra el backend REAL (vms.api.create_app)
en lugar del backend de pruebas. Se activa con la variable VMS_TEST_WEB_BACKEND=real.

Expone los mismos atributos que usa la suite (`engine`, `creds`, `opt.kiosk_token`, `paths`, `app`)
y conecta los mismos dobles: el motor que se le pase (FakeEngine o MtxTestEngine), clientes de
equipo de prueba y una búsqueda en red simulada.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from tests.conftest import get_free_port
from tests.web.stub_backend import DEFAULT_USERS, StubOptions
from vms.core.credentials import CredentialStore, EncryptedFileBackend
from vms.core.errors import VmsError
from vms.core.interfaces import DeviceClient, DeviceTestResult, Engine
from vms.core.models import User
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings


@dataclass
class _Opt:
    kiosk_token: str


def _tester(factory: Callable[[Any, str], DeviceClient]) -> Callable[[Any, str], Awaitable[DeviceTestResult]]:
    async def test(device: Any, password: str) -> DeviceTestResult:
        client = factory(device, password)
        try:
            info = await client.probe()
            chans = await client.list_channels()
            return DeviceTestResult(ok=True, reachable=True, auth_ok=True, rtsp_ok=True, info=info, channels=chans,
                                    message=f"{info.model or 'Equipo'} con {len(chans)} canales")
        except VmsError as exc:
            return DeviceTestResult(ok=False, reachable=exc.code != "device_unreachable",
                                    auth_ok=False if exc.code == "device_auth_failed" else None, message=exc.message)
        finally:
            await client.aclose()
    return test


class RealBackend:
    def __init__(self, data_dir: Path, engine: Engine, options: StubOptions | None = None) -> None:
        from tests.fakes import FakeDeviceClient
        from vms.api.app import create_app
        from vms.api.security import hash_password

        opt = options or StubOptions()
        self.paths = AppPaths(Path(data_dir)).ensure()
        self.engine = engine
        self.opt = _Opt(kiosk_token=opt.kiosk_token or "kiosk-token-pruebas")
        key = EncryptedFileBackend.load_or_create_key(self.paths.secrets_dir)
        self.creds = CredentialStore(EncryptedFileBackend(self.paths.secrets_dir / "credentials.enc", key))
        settings = VmsSettings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=self.paths.base, site_id="site-test", http_host="127.0.0.1", http_port=get_free_port(),
            internal_token="token-interno-de-pruebas", kiosk_token=self.opt.kiosk_token, credential_backend="file")

        factory = opt.device_client or (lambda d, p: FakeDeviceClient(d.vendor, channels=4 if d.kind == "nvr" else 1,
                                                                       kind=d.kind))
        self.app = create_app(settings, engine=engine, credential_store=self.creds, client_factory=factory,
                              device_tester=_tester(factory), discoverer=opt.discover or _no_discovery,
                              heartbeat=False, apply_delay=0.2)
        state = self.app.state.vms
        async def seed() -> None:
            for username, (password, role) in DEFAULT_USERS.items():
                await state.users.save_user(User(username=username, role=role,  # type: ignore[arg-type]
                                                 password_hash=hash_password(password)))

        # en otro hilo: el API síncrono de Playwright mantiene un bucle asyncio en el hilo principal
        with ThreadPoolExecutor(1) as pool:
            pool.submit(asyncio.run, seed()).result()


async def _no_discovery(timeout: float) -> list[Any]:
    return []
