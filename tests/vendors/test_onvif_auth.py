"""ONVIF: un solo intento con credenciales también cuando el equipo autentica por Digest HTTP (criterio 4 de B5).

Casos de la revisión: (a) el equipo responde 401 + Digest a un UsernameToken malo (antes se reenviaba el mismo
sobre firmado con Digest: 2 intentos); (b) el equipo pide Digest hasta para la hora y la contraseña es mala
(antes acababa en «async generator raised StopIteration»).
"""
from __future__ import annotations

import httpx
import pytest

from tools.mocks.onvif import OnvifMock
from vms.core.errors import DeviceAuthFailed
from vms.vendors import client_for, test_device
from vms.vendors._http import VendorAuth
from vms.vendors.errors import BasicNotAllowed
from vms.vendors.onvif_client import OnvifClient

from .conftest import TEST_PASSWORD, asgi, make_device

SCENARIOS = {
    "fault-ws": {},                                                       # Fault NotAuthorized (lo habitual)
    "401-digest-ante-token": {"http_auth": "digest"},                     # 401 + reto Digest ante token malo
    "solo-digest": {"http_auth": "digest", "ws_token": False},           # ignora el UsernameToken
    "digest-hasta-la-hora": {"http_auth": "digest", "clock_needs_auth": True},
    "digest-sha256-hasta-la-hora": {"http_auth": "digest-sha256", "clock_needs_auth": True, "ws_token": False},
}


def _client(mock: OnvifMock, password: str) -> OnvifClient:
    client = client_for(make_device("onvif"), password, transport=asgi(mock.app))
    assert isinstance(client, OnvifClient)
    return client


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_bad_password_is_sent_exactly_once(scenario: str) -> None:
    mock = OnvifMock(password=TEST_PASSWORD, media2=True, **SCENARIOS[scenario])  # type: ignore[arg-type]
    client = _client(mock, "mala")
    try:
        with pytest.raises(DeviceAuthFailed):
            await client.probe()
        # fundido: lo que venga después tampoco manda la contraseña (ni por la otra vía)
        with pytest.raises(DeviceAuthFailed):
            await client.list_channels()
        with pytest.raises(DeviceAuthFailed):
            await client.probe()
    finally:
        await client.aclose()
    assert mock.credentialed == 1, mock.operations
    assert client.credentialed_requests == 1


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_good_password_works_in_every_auth_mode(scenario: str) -> None:
    mock = OnvifMock(password=TEST_PASSWORD, media2=True, **SCENARIOS[scenario])  # type: ignore[arg-type]
    client = _client(mock, TEST_PASSWORD)
    try:
        info = await client.probe()
        chans = await client.list_channels()
        t = await client.device_time()
    finally:
        await client.aclose()
    assert info.model == mock.model and chans[0].main_path == "/Streaming/Channels/101"
    assert t.source == "onvif" and t.time_mode == "ntp"
    assert mock.rejected == 0


async def test_device_check_reports_bad_password_not_runtime_error() -> None:
    mock = OnvifMock(password=TEST_PASSWORD, http_auth="digest", clock_needs_auth=True)
    res = await test_device(make_device("onvif"), "mala", transport=asgi(mock.app), first_frame=False)
    assert res.auth_ok is False
    assert "RuntimeError" not in res.message and "inesperado" not in res.message.lower()
    assert "contraseña" in res.message.lower()
    assert mock.credentialed == 1


async def test_basic_only_onvif_is_refused_without_sending_the_password() -> None:
    mock = OnvifMock(password=TEST_PASSWORD, http_auth="basic", ws_token=False)
    client = _client(mock, TEST_PASSWORD)
    try:
        with pytest.raises(BasicNotAllowed):
            await client.probe()
    finally:
        await client.aclose()
    assert mock.credentialed == 0


async def test_rejected_vendor_auth_raises_instead_of_empty_generator() -> None:
    """Un VendorAuth fundido no puede dejar a httpx con un generador vacío (RuntimeError)."""
    auth = VendorAuth("admin", "mala")
    auth.rejected = True
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as c:
        with pytest.raises(DeviceAuthFailed):
            await c.get("http://10.0.0.5/")
    assert sent == []
