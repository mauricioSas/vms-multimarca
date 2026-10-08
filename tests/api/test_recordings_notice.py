"""Línea de tiempo y vídeo que no mienten (CONTRATO §6.7).

- Una cámara que nunca grabó, desactivada o con la grabación apagada lo dice (`notice` con «Qué hacer:») en vez de
  un «Sin grabaciones este día» mudo.
- Sin carpeta de grabaciones (disco externo desconectado) es un error 502, nunca «nunca ha grabado».
- Motor caído → 503 `engine_unavailable`; motor que contesta con error → 502 `engine_error`.
- /video de una cámara que nunca grabó (MediaMTX: 400 «cannot find the path specified») → 404, no 400.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tests.api.conftest import Harness, new_device
from vms.api.routes.recordings import NO_RECORDING_THERE
from vms.core.errors import EngineError, EngineUnavailable
from vms.core.recordings import PLAYBACK_DENIED, PLAYBACK_ENGINE_DOWN, PLAYBACK_NOT_CONFIGURED, RECORDINGS_DIR_MISSING

LONG_MISSING = ("GetFileAttributesEx C:\\ProgramData\\VMSMultimarca\\recordings\\" + "carpeta-larga\\" * 25 +
                "cam-ca3b7c25\\main: The system cannot find the path specified.")


@pytest.fixture
async def cam(api: Harness) -> str:
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1, 2])
    await api.settle()
    return str(dev["cameras"][0])


def rec_root(api: Harness) -> Path:
    return Path(api.state.recordings_dir())


async def timeline(api: Harness, cam: str) -> Any:
    op = await api.operator()
    return await op.get(f"/api/recordings/{cam}/timeline")


async def test_camera_that_never_recorded_says_so(api: Harness, cam: str) -> None:
    api.engine.spans[cam] = []
    rec_root(api).mkdir(parents=True, exist_ok=True)
    r = await timeline(api, cam)
    assert r.status_code == 200
    body = r.json()
    assert body["spans"] == [] and body["notice"]["code"] == "never_recorded"
    assert "Qué hacer:" in body["notice"]["message"]
    (rec_root(api) / cam / "main").mkdir(parents=True)
    body = (await timeline(api, cam)).json()
    assert body["spans"] == [] and body["notice"] is None   # ha grabado alguna vez: ese día no hay nada


async def test_recordings_found_have_no_notice(api: Harness, cam: str) -> None:
    body = (await timeline(api, cam)).json()
    assert body["spans"] and body["notice"] is None


async def test_recording_off_and_disabled_camera(api: Harness, cam: str) -> None:
    api.engine.spans[cam] = []
    rec_root(api).mkdir(parents=True, exist_ok=True)
    admin = await api.login()
    assert (await admin.patch(f"/api/cameras/{cam}", json={"record": False})).status_code == 200
    body = (await timeline(api, cam)).json()
    assert body["notice"]["code"] == "recording_off" and "Grabar 24/7" in body["notice"]["message"]
    assert (await admin.patch(f"/api/cameras/{cam}", json={"enabled": False})).status_code == 200
    body = (await timeline(api, cam)).json()
    assert body["notice"]["code"] == "camera_disabled" and "Qué hacer:" in body["notice"]["message"]


async def test_missing_recordings_root_is_an_error_not_never_recorded(api: Harness, cam: str) -> None:
    api.engine.spans[cam] = []
    shutil.rmtree(rec_root(api), ignore_errors=True)
    r = await timeline(api, cam)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["code"] == "engine_error" and err["message"] == RECORDINGS_DIR_MISSING
    assert "never_recorded" not in r.text


async def test_engine_down_and_engine_error_are_told_apart(api: Harness, cam: str) -> None:
    api.engine.list_errors[cam] = EngineUnavailable(PLAYBACK_ENGINE_DOWN)
    r = await timeline(api, cam)
    assert r.status_code == 503 and r.json()["error"]["code"] == "engine_unavailable"
    assert "Qué hacer:" in r.json()["error"]["message"]
    api.engine.list_errors[cam] = EngineError(PLAYBACK_DENIED, details={"kind": "denied"})
    r = await timeline(api, cam)
    assert r.status_code == 502 and r.json()["error"] == {"code": "engine_error", "message": PLAYBACK_DENIED,
                                                          "details": {"kind": "denied"}}


async def test_summary_survives_one_camera_with_an_engine_error(api: Harness, cam: str) -> None:
    api.engine.list_errors[cam] = EngineError(PLAYBACK_DENIED)
    op = await api.operator()
    r = await op.get("/api/recordings/summary")
    assert r.status_code == 200
    rows = {row["camera_id"]: row for row in r.json()}
    assert rows[cam]["first"] is None and len(rows) == 2
    assert any(row["first"] is not None for row in rows.values())


# --------------------------------------------------------------------------------------------- /video
@pytest.fixture
async def playback_server(api: Harness, mock_server: Callable[[Any], Any]) -> list[str]:
    calls: list[str] = []

    async def get(request: Request) -> Response:
        calls.append(str(request.url))
        start = request.query_params.get("start", "")
        if start.startswith("2026-10-08"):      # MediaMTX real con la carpeta de la cámara inexistente
            error = LONG_MISSING
        elif start.startswith("2026-10-07"):
            error = "open C:\\rec\\cam-ca3b7c25\\main: Access is denied."
        elif start.startswith("2026-10-05"):     # cámara sin ruta en el motor (ni activa ni inerte)
            error = f"path '{request.query_params.get('path', '')}' is not configured"
        else:
            error = "invalid duration"
        return Response(json.dumps({"status": "error", "error": error}), 400, media_type="application/json")

    srv = mock_server(Starlette(routes=[Route("/get", get)]))

    def get_url(cid: str, start: datetime, duration: float, fmt: str = "fmp4") -> str:
        return f"{srv.base_url}/get?path={cid}/main&start={start.isoformat()}&duration={duration}&format={fmt}"

    api.engine.playback_get_url = get_url  # type: ignore[method-assign,assignment]
    return calls


async def test_video_of_a_camera_that_never_recorded_is_404(api: Harness, cam: str, playback_server: list[str]) -> None:
    assert len(LONG_MISSING) > 300   # antes solo se leían 300 bytes y se devolvía 400 «playback_rejected»
    op = await api.operator()
    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-08T10:00:00Z", "duration": 10})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-07T10:00:00Z", "duration": 10})
    assert r.status_code == 502 and r.json()["error"]["message"] == PLAYBACK_DENIED
    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-06T10:00:00Z", "duration": 10})
    assert r.status_code == 400 and r.json()["error"]["code"] == "playback_rejected"
    assert r.json()["error"]["details"]["reason"] == "invalid duration"


async def test_video_of_a_disabled_camera_without_recordings_is_404(api: Harness, cam: str,
                                                                    playback_server: list[str]) -> None:
    """Desactivada y sin grabaciones no tiene ruta en MediaMTX (ni inerte): antes, 502 «espera unos segundos y pulsa
    Actualizar», que nunca se arregla solo. Activada, «no configurada» sí es un retraso del motor."""
    op = await api.operator()
    params = {"start": "2026-10-05T10:00:00Z", "duration": 10}
    r = await op.get(f"/api/recordings/{cam}/video", params=params)
    assert r.status_code == 502 and r.json()["error"]["message"] == PLAYBACK_NOT_CONFIGURED
    admin = await api.login()
    assert (await admin.patch(f"/api/cameras/{cam}", json={"enabled": False})).status_code == 200
    r = await op.get(f"/api/recordings/{cam}/video", params=params)
    assert r.status_code == 404 and r.json()["error"]["message"] == NO_RECORDING_THERE


async def test_video_with_the_engine_down_says_what_to_do(api: Harness, cam: str) -> None:
    api.engine.playback_get_url = (  # type: ignore[method-assign]
        lambda cid, start, duration, fmt="fmp4": f"http://127.0.0.1:1/get?path={cid}/main")
    op = await api.operator()
    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-08T10:00:00Z", "duration": 10})
    assert r.status_code == 503 and r.json()["error"]["message"] == PLAYBACK_ENGINE_DOWN
