"""Lo que B6 necesita del backend, como Protocol (sin importar `vms.api` ni `vms.engine`).

`vms/ops` se revisa con mypy en CI (`mypy vms/core vms/ops`); importar `vms.api.state` o `vms.engine` arrastraría
a la revisión módulos de otros bloques. El backend real (`vms.api.state.AppState`) cumple este Protocol y las
pruebas lo comprueban (`tests/ops/test_host_contract.py`).

Nombres de los eventos SSE de B6: los mismos que declara `vms/api/events.py` (CONTRATO §18.18, congelados).
"""
from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from vms.core.credentials import CredentialStore
from vms.core.interfaces import DeviceClient, Engine, PathStatus
from vms.core.models import AppConfig, Device, Site
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings

EVENT_HEALTH = "health"
EVENT_BOOKMARK = "bookmark"
EVENT_EVIDENCE = "evidence"
EVENT_NOTICE = "notice"


class EventSink(Protocol):
    def publish(self, event: str, data: dict[str, Any]) -> None: ...


class OpsHost(Protocol):
    settings: VmsSettings
    paths: AppPaths
    creds: CredentialStore
    engine: Engine
    client_factory: Callable[[Device, str], DeviceClient]
    bus: EventSink
    proxy: Any

    def config(self) -> AppConfig: ...
    def site(self) -> Site: ...
    def recordings_dir(self, cfg: AppConfig | None = None) -> str: ...
    def overview(self) -> Awaitable[dict[str, Any]]: ...
    def paths_status_safe(self) -> Awaitable[dict[str, PathStatus] | None]: ...


def dynamic(module: str, attr: str) -> Any:
    """Atributo de un módulo de otro bloque, cargado al usarlo (sin que mypy revise ese módulo)."""
    return getattr(importlib.import_module(module), attr)
