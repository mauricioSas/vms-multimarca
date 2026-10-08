"""Respuestas del servidor de reproducción de MediaMTX → «sin grabaciones», error del motor o motor caído.

Los cuerpos son los que devuelve MediaMTX v1.21.1 real en Windows (sondas con `bin\\mediamtx.exe`). Antes, cualquier
400 era «sin grabaciones» (una carpeta que el motor no puede leer quedaba escondida) y un 401 o un 500 con el motor
en marcha salía como «motor caído», sin «Qué hacer:».
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from vms.core.errors import EngineError, EngineUnavailable
from vms.core.recordings import (PLAYBACK_AUTH, PLAYBACK_DENIED, PLAYBACK_ENGINE_DOWN, PLAYBACK_FAILED,
                                 PLAYBACK_NOT_CONFIGURED, RECORDINGS_DIR_MISSING, classify_playback_error,
                                 playback_error_text, playback_says_no_recordings)
from vms.engine.mtx_api import MediaMtxApi

LONG_DIR = "C:\\ProgramData\\VMSMultimarca\\recordings\\" + "carpeta-muy-larga\\" * 20 + "cam-ca3b7c25\\main"
MISSING = f"GetFileAttributesEx {LONG_DIR}: The system cannot find the path specified."
DENIED = "open C:\\ProgramData\\VMSMultimarca\\recordings\\cam-ca3b7c25\\main: Access is denied."


def body(error: str) -> str:
    return json.dumps({"status": "error", "error": error})


def api_answering(status: int, text: str) -> tuple[MediaMtxApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=text, headers={"Content-Type": "application/json"})

    api = MediaMtxApi("http://127.0.0.1:1", "http://127.0.0.1:2")
    api.playback = httpx.AsyncClient(base_url="http://127.0.0.1:2", transport=httpx.MockTransport(handler))
    return api, seen


@pytest.mark.parametrize("status,error", [
    (400, MISSING),                                          # carpeta de la cámara inexistente: nunca grabó
    (404, "no recording segments found"),                    # carpeta vacía o nada en ese día
])
async def test_no_recordings_is_an_empty_list(status: int, error: str) -> None:
    api, seen = api_answering(status, body(error))
    assert await api.list_recordings("cam-ca3b7c25/main", datetime(2026, 10, 8, tzinfo=timezone.utc), None) == []
    assert seen and seen[0].url.params["path"] == "cam-ca3b7c25/main"
    await api.aclose()


@pytest.mark.parametrize("status,error,message,kind", [
    (400, DENIED, PLAYBACK_DENIED, "denied"),                                    # ACL de la carpeta (hipótesis c)
    (400, "path 'cam-ca3b7c25/main' is not configured", PLAYBACK_NOT_CONFIGURED, "not_configured"),
    (401, "authentication error", PLAYBACK_AUTH, "auth"),                        # credenciales internas
    (500, "internal error", PLAYBACK_FAILED, "other"),
    (400, "invalid start: parsing time \"ayer\"", PLAYBACK_FAILED, "other"),
])
async def test_engine_answering_with_an_error_is_a_real_error(status: int, error: str, message: str,
                                                              kind: str) -> None:
    api, _ = api_answering(status, body(error))
    with pytest.raises(EngineError) as info:
        await api.list_recordings("cam-ca3b7c25/main", None, None)
    exc = info.value
    assert (exc.code, exc.status, exc.message) == ("engine_error", 502, message)
    assert exc.details["status"] == status and exc.details["kind"] == kind
    assert exc.details["reason"] == error[:200]
    assert "Qué hacer:" in exc.message
    await api.aclose()


async def test_engine_down_says_so_with_what_to_do() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexión rechazada", request=request)

    api = MediaMtxApi("http://127.0.0.1:1", "http://127.0.0.1:2")
    api.playback = httpx.AsyncClient(base_url="http://127.0.0.1:2", transport=httpx.MockTransport(refuse))
    with pytest.raises(EngineUnavailable) as info:
        await api.list_recordings("cam-ca3b7c25/main", None, None)
    assert info.value.status == 503 and info.value.message == PLAYBACK_ENGINE_DOWN
    assert "Qué hacer:" in info.value.message and info.value.details == {"reason": "ConnectError"}
    await api.aclose()


@pytest.mark.parametrize("error", [
    MISSING,
    "GetFileAttributesEx D:\\rec\\cam-x\\main: El sistema no puede encontrar la ruta especificada.",
    "lstat /var/lib/vms/recordings/cam-x/main: no such file or directory",
])
def test_missing_folder_wording_in_english_spanish_and_linux(error: str) -> None:
    assert playback_says_no_recordings(400, error)
    assert classify_playback_error(400, error) is None


def test_only_missing_folder_400s_are_empty() -> None:
    assert not playback_says_no_recordings(400, DENIED)
    assert not playback_says_no_recordings(400, "path 'x' is not configured")
    assert not playback_says_no_recordings(500, MISSING)
    assert playback_says_no_recordings(404, "")


def test_error_text_reads_the_json_field_or_the_raw_body() -> None:
    assert playback_error_text(body(MISSING).encode()) == MISSING
    assert playback_error_text("texto plano\n") == "texto plano"
    assert playback_error_text(b'{"otra": 1}') == '{"otra": 1}'


def test_every_playback_message_says_what_to_do() -> None:
    for msg in (PLAYBACK_AUTH, PLAYBACK_DENIED, PLAYBACK_ENGINE_DOWN, PLAYBACK_FAILED, PLAYBACK_NOT_CONFIGURED,
                RECORDINGS_DIR_MISSING):
        assert "Qué hacer:" in msg
