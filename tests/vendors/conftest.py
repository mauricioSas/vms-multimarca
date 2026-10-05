"""Dobles y ayudas de las pruebas de drivers (B5): servidor RTSP caótico y equipos de prueba."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tools.mocks.rtsp_chaos import RtspChaosServer
from vms.core.models import Device, DeviceKind

TEST_PASSWORD = "Sim#Pass:1@/x"     # caracteres conflictivos (PLAN-V2 §3.3, prueba 8)


@pytest.fixture
async def chaos() -> AsyncIterator[RtspChaosServer]:
    async with RtspChaosServer(password=TEST_PASSWORD, slow_first_frame_s=5.0) as srv:
        yield srv


def make_device(vendor: str, *, kind: DeviceKind = "camera", rtsp_port: int = 554, host: str = "127.0.0.1",
                **extra: Any) -> Device:
    extra.setdefault("username", "admin")
    return Device(name=f"Equipo {vendor}", vendor=vendor, kind=kind, host=host, rtsp_port=rtsp_port,
                  http_port=80, **extra)


def asgi(app: Any) -> httpx.AsyncBaseTransport:
    return httpx.ASGITransport(app=app)
