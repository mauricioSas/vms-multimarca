"""Proxy WHEP y proxy de reproducción contra un MediaMTX de imitación (CONTRATO §6.6-6.7)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tests.api.conftest import Harness, new_device
from vms.core.interfaces import RecordingSpan

SDP_ANSWER = b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\n"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x01" * 200_000


class FakeMediaMtx:
    """Imita el WHEP y el /get de MediaMTX para probar el proxy sin WebRTC real."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes, dict[str, str]]] = []

        async def whep(request: Request) -> Response:
            body = await request.body()
            self.calls.append((request.method, request.url.path, body, dict(request.headers)))
            if request.method == "OPTIONS":
                return Response(status_code=204, headers={"Link": '<stun:stun.example:3478>; rel="ice-server"',
                                                          "Accept-Post": "application/sdp"})
            if request.path_params["cam"] == "cam-0000dead" or b"no-configurada" in body:
                # MediaMTX real entre el alta de la cámara y la aplicación de la configuración
                return Response('{"status":"error","error":"path \'x/sub\' is not configured"}', 500,
                                media_type="application/json")
            if b"rechazar" in body:
                return Response('{"status":"error","error":"SetRemoteDescription failed"}', 400,
                                media_type="application/json")
            return Response(SDP_ANSWER, 201, media_type="application/sdp",
                            headers={"Location": f"{request.url.path}/3e83e256-aaaa-4bbb-8ccc-0123456789ab",
                                     "ETag": '"abc"', "Accept-Patch": "application/trickle-ice-sdpfrag"})

        async def session(request: Request) -> Response:
            self.calls.append((request.method, request.url.path, await request.body(), dict(request.headers)))
            return Response(status_code=204 if request.method == "PATCH" else 200)

        async def get(request: Request) -> Response:
            self.calls.append(("GET", str(request.url), b"", {}))
            if request.query_params.get("start", "").startswith("2020"):
                return Response('{"status":"error","error":"no recording segments found"}', 404)
            return Response(MP4, media_type="video/mp4")

        self.app = Starlette(routes=[
            Route("/{cam}/{stream}/whep", whep, methods=["OPTIONS", "POST"]),
            Route("/{cam}/{stream}/whep/{sid}", session, methods=["PATCH", "DELETE"]),
            Route("/get", get),
        ])


@pytest.fixture
async def setup(api: Harness, mock_server: Callable[[Any], Any]) -> tuple[Harness, FakeMediaMtx, str]:
    fake = FakeMediaMtx()
    srv = mock_server(fake.app)
    api.engine.whep_url = lambda cid, stream: f"{srv.base_url}/{cid}/{stream}/whep"  # type: ignore[method-assign]

    def get_url(cid: str, start: datetime, duration: float, fmt: str = "fmp4") -> str:
        return f"{srv.base_url}/get?path={cid}/main&start={start.isoformat()}&duration={duration}&format={fmt}"

    api.engine.playback_get_url = get_url  # type: ignore[method-assign,assignment]
    admin = await api.login()
    dev = await new_device(admin, import_channels=[1])
    await api.settle()
    return api, fake, dev["cameras"][0]


async def test_live_info_and_whep_negotiation_via_proxy(setup: tuple[Harness, FakeMediaMtx, str]) -> None:
    api, fake, cam = setup
    k = await api.kiosk()  # los muros en kiosco pueden ver en vivo
    info = (await k.get(f"/api/live/{cam}")).json()
    assert info == {"camera_id": cam, "stream": "sub", "whep_url": f"/api/live/{cam}/sub/whep",
                    "ready": True, "tracks": ["H264"]}
    assert (await k.get(f"/api/live/{cam}", params={"stream": "main"})).json()["stream"] == "main"

    r = await k.options(f"/api/live/{cam}/sub/whep")
    assert r.status_code == 204 and "ice-server" in r.headers["link"]

    r = await k.post(f"/api/live/{cam}/sub/whep", content=b"v=0\r\noferta\r\n",
                     headers={"Content-Type": "application/sdp"})
    assert r.status_code == 201 and r.content == SDP_ANSWER
    assert r.headers["content-type"].startswith("application/sdp")
    loc = r.headers["location"]
    assert loc == f"/api/live/{cam}/sub/whep/3e83e256-aaaa-4bbb-8ccc-0123456789ab"
    assert r.headers["etag"] == '"abc"'
    method, path, body, headers = fake.calls[-1]
    assert (method, path, body) == ("POST", f"/{cam}/sub/whep", b"v=0\r\noferta\r\n")
    assert "cookie" not in headers, "la cookie de sesión no se reenvía al motor"

    r = await k.patch(loc, content=b"a=candidate:1 1 udp 1 1.2.3.4 5 typ host\r\n",
                      headers={"Content-Type": "application/trickle-ice-sdpfrag", "If-Match": '"abc"'})
    assert r.status_code == 204
    assert fake.calls[-1][0] == "PATCH" and fake.calls[-1][3].get("if-match") == '"abc"'
    assert (await k.delete(loc)).status_code == 200
    assert (await k.delete(f"/api/live/{cam}/sub/whep/..%2F..%2Fx")).status_code == 404

    r = await k.post(f"/api/live/{cam}/sub/whep", content=b"rechazar", headers={"Content-Type": "application/sdp"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "whep_rejected"
    r = await k.post(f"/api/live/{cam}/sub/whep", content=b"x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = await k.post(f"/api/live/{cam}/sub/whep", content=b"x", headers={"Content-Type": "application/sdp",
                                                                         "X-Requested-With": ""})
    assert r.status_code == 403  # CSRF también en WHEP
    assert (await k.get("/api/live/cam-00000000")).status_code == 404
    anon = api.client()
    assert (await anon.post(f"/api/live/{cam}/sub/whep", content=b"x",
                            headers={"Content-Type": "application/sdp"})).status_code == 401


async def test_camera_without_substream_uses_main(setup: tuple[Harness, FakeMediaMtx, str]) -> None:
    api, fake, cam = setup
    admin = await api.login()
    await admin.patch(f"/api/cameras/{cam}", json={"has_sub": False})
    info = (await admin.get(f"/api/live/{cam}")).json()
    assert info["stream"] == "main" and info["whep_url"].endswith("/main/whep")
    r = await admin.post(f"/api/live/{cam}/sub/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
    assert r.status_code == 201 and fake.calls[-1][1] == f"/{cam}/main/whep"


async def test_timeline_video_and_download(setup: tuple[Harness, FakeMediaMtx, str]) -> None:
    api, fake, cam = setup
    start = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    api.engine.spans[cam] = [RecordingSpan(start=start, duration=3600.0),
                             RecordingSpan(start=start + timedelta(hours=2), duration=60.0)]
    op = await api.operator()
    r = await op.get(f"/api/recordings/{cam}/timeline",
                     params={"start": "2026-10-04T11:30:00+02:00", "end": "2026-10-04T13:00:00Z"})
    assert r.status_code == 200
    spans = r.json()["spans"]
    assert spans == [{"start": "2026-10-04T10:00:00Z", "end": "2026-10-04T11:00:00Z", "duration": 3600.0},
                     {"start": "2026-10-04T12:00:00Z", "end": "2026-10-04T12:01:00Z", "duration": 60.0}]
    bad = await op.get(f"/api/recordings/{cam}/timeline", params={"start": "2026-10-04T13:00:00Z",
                                                                  "end": "2026-10-04T12:00:00Z"})
    assert bad.status_code == 422

    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-04T12:00:05+02:00", "duration": 30,
                                                             "format": "mp4"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and r.content == MP4
    assert "start=2026-10-04T10:00:05+00:00" in fake.calls[-1][1] and "format=mp4" in fake.calls[-1][1]
    assert "content-disposition" not in r.headers

    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-04T10:00:05Z", "duration": 10,
                                                             "download": 1})
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;") and 'filename="Camara_1_20261004-120005.mp4"' in disp  # hora de la tienda
    assert "filename*=UTF-8''C%C3%A1mara%201_20261004-120005.mp4" in disp

    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2020-01-01T00:00:00Z", "duration": 10})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "2026-10-04T10:00:00Z", "duration": 3601})
    assert r.status_code == 422
    r = await op.get(f"/api/recordings/{cam}/video", params={"start": "ayer", "duration": 10})
    assert r.status_code == 422
    k = await api.kiosk()
    assert (await k.get(f"/api/recordings/{cam}/timeline")).status_code == 403

    summary = (await op.get("/api/recordings/summary")).json()
    assert summary[0]["camera_id"] == cam and summary[0]["first"] == "2026-10-04T10:00:00Z"
    assert summary[0]["last"] == "2026-10-04T12:01:00Z" and summary[0]["bytes"] == 0


async def test_whep_path_not_configured_is_404_not_500(setup: tuple[Harness, FakeMediaMtx, str]) -> None:
    """Recién dada de alta, MediaMTX aún no tiene la ruta: el muro debe ver «sin vídeo» (404) y reintentar."""
    api, _, cam = setup
    k = await api.login()
    r = await k.post(f"/api/live/{cam}/sub/whep", content=b"v=0 no-configurada",
                     headers={"Content-Type": "application/sdp"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "stream_not_available"
