"""Vista en vivo: proxy WHEP hacia MediaMTX (CONTRATO §6.6).

El navegador solo habla con el backend (misma cookie de sesión, sin CORS). Aquí solo viaja la
negociación SDP/ICE; el vídeo va directo del puerto ICE de MediaMTX al navegador.
"""
from __future__ import annotations

import logging
import re
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import EngineUnavailable, NotFoundError, ValidationFailed

from ..deps import Principal, get_state, require_kiosk
from ..errors import error_response, json_response
from ..security import client_ip
from ..state import AppState
from .cameras import get_camera

log = logging.getLogger("vms.api.live")
router = APIRouter(prefix="/api/live", tags=["live"])

MAX_SDP_BYTES = 64 * 1024
SESSION_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
PASS_HEADERS = ("link", "etag", "accept-patch", "accept-post")
Stream = Literal["sub", "main"]


def _effective_stream(state: AppState, camera_id: str, stream: str) -> Literal["sub", "main"]:
    cam = get_camera(state.config(), camera_id)
    if stream not in ("sub", "main"):
        raise NotFoundError("Flujo no válido (usa «sub» o «main»)")
    return "main" if stream == "main" or not cam.has_sub else "sub"


def _client(state: AppState) -> httpx.AsyncClient:
    if state.proxy is None:
        raise EngineUnavailable("El proxy de vídeo no está listo")
    return state.proxy


def _upstream_error(resp: httpx.Response) -> Response:
    try:
        msg = str(resp.json().get("error", ""))
    except ValueError:
        msg = resp.text[:200]
    if resp.status_code == 404 or _path_not_configured(resp.status_code, msg):
        # «path is not configured»: la cámara se acaba de dar de alta y el motor aún no tiene su
        # ruta (antirrebote de la configuración). Para el muro es lo mismo que «sin vídeo»: reintenta.
        return error_response("stream_not_available",
                              "La cámara no está enviando vídeo ahora mismo", 404, {"reason": msg})
    if resp.status_code == 400:
        return error_response("whep_rejected", "El motor de vídeo rechazó la negociación WebRTC", 400, {"reason": msg})
    return error_response("engine_error", "El motor de vídeo devolvió un error", 502,
                          {"status": resp.status_code, "reason": msg})


def _path_not_configured(status: int, msg: str) -> bool:
    return status >= 400 and "not configured" in msg.lower()


def _pass_headers(resp: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in PASS_HEADERS:
        values = resp.headers.get_list(name)
        if values:
            out[name.title()] = ", ".join(values)
    return out


@router.get("/{camera_id}")
async def live_info(camera_id: str, stream: Stream = "sub", _: Principal = Depends(require_kiosk),
                    state: AppState = Depends(get_state)) -> Response:
    eff = _effective_stream(state, camera_id, stream)
    paths = await state.paths_status_safe()
    st = (paths or {}).get(f"{camera_id}/{eff}")
    main = (paths or {}).get(f"{camera_id}/main")
    return json_response({
        "camera_id": camera_id, "stream": eff, "whep_url": f"/api/live/{camera_id}/{eff}/whep",
        "ready": bool(st and st.ready) or (eff == "sub" and bool(main and main.ready)),
        "tracks": list(st.tracks) if st and st.tracks else (list(main.tracks) if main else []),
    })


@router.options("/{camera_id}/{stream}/whep")
async def whep_options(camera_id: str, stream: str, _: Principal = Depends(require_kiosk),
                       state: AppState = Depends(get_state)) -> Response:
    eff = _effective_stream(state, camera_id, stream)
    try:
        resp = await _client(state).options(state.engine.whep_url(camera_id, eff))
    except httpx.HTTPError as exc:
        raise EngineUnavailable("El motor de vídeo no responde") from exc
    if resp.status_code >= 300:
        try:
            msg = str(resp.json().get("error", ""))
        except ValueError:
            msg = resp.text[:200]
        if resp.status_code == 404 or _path_not_configured(resp.status_code, msg):
            return Response(status_code=404, headers=_pass_headers(resp))
        return Response(status_code=resp.status_code, headers=_pass_headers(resp))
    return Response(status_code=204, headers=_pass_headers(resp))


@router.post("/{camera_id}/{stream}/whep")
async def whep_offer(camera_id: str, stream: str, request: Request, p: Principal = Depends(require_kiosk),
                     state: AppState = Depends(get_state)) -> Response:
    eff = _effective_stream(state, camera_id, stream)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/sdp":
        return error_response("unsupported_media_type", "La oferta debe enviarse como application/sdp", 415)
    body = await request.body()
    if not body or len(body) > MAX_SDP_BYTES:
        raise ValidationFailed("Oferta SDP vacía o demasiado grande")
    try:
        resp = await _client(state).post(state.engine.whep_url(camera_id, eff), content=body,
                                         headers={"Content-Type": "application/sdp"}, timeout=20.0)
    except httpx.HTTPError as exc:
        raise EngineUnavailable("El motor de vídeo no responde") from exc
    if resp.status_code != 201:
        log.info("WHEP rechazado para %s/%s: %s", camera_id, eff, resp.status_code)
        return _upstream_error(resp)
    location = resp.headers.get("location", "")
    session = location.rstrip("/").rsplit("/", 1)[-1] if "/whep/" in location else ""
    if not SESSION_RE.match(session):
        log.error("MediaMTX devolvió una sesión WHEP con formato inesperado")
        return error_response("engine_error", "Respuesta WHEP inesperada del motor de vídeo", 502)
    if eff == "main":  # vista en vivo a resolución completa (pantalla completa o 1 celda)
        audit("live_main", user=p.username, ip=client_ip(request), camera_id=camera_id)
    headers = _pass_headers(resp)
    headers["Location"] = f"/api/live/{camera_id}/{eff}/whep/{session}"
    headers["Cache-Control"] = "no-store"
    return Response(resp.content, status_code=201, media_type="application/sdp", headers=headers)


async def _session_call(state: AppState, method: str, camera_id: str, stream: str, session: str,
                        request: Request) -> httpx.Response:
    eff = _effective_stream(state, camera_id, stream)
    if not SESSION_RE.match(session):
        raise NotFoundError("Sesión WHEP no válida")
    url = f"{state.engine.whep_url(camera_id, eff)}/{session}"
    headers = {}
    for name in ("content-type", "if-match"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    body = await request.body()
    if len(body) > MAX_SDP_BYTES:
        raise ValidationFailed("Fragmento ICE demasiado grande")
    try:
        return await _client(state).request(method, url, content=body or None, headers=headers)
    except httpx.HTTPError as exc:
        raise EngineUnavailable("El motor de vídeo no responde") from exc


@router.patch("/{camera_id}/{stream}/whep/{session}")
async def whep_patch(camera_id: str, stream: str, session: str, request: Request,
                     _: Principal = Depends(require_kiosk), state: AppState = Depends(get_state)) -> Response:
    resp = await _session_call(state, "PATCH", camera_id, stream, session, request)
    if resp.status_code >= 300:
        return _upstream_error(resp)
    return Response(status_code=204)


@router.delete("/{camera_id}/{stream}/whep/{session}")
async def whep_delete(camera_id: str, stream: str, session: str, request: Request,
                      _: Principal = Depends(require_kiosk), state: AppState = Depends(get_state)) -> Response:
    resp = await _session_call(state, "DELETE", camera_id, stream, session, request)
    if resp.status_code >= 300 and resp.status_code != 404:
        return _upstream_error(resp)
    return Response(status_code=200)
