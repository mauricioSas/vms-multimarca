"""Registro de drivers y acciones de B5 sobre equipos (CONTRATO §16.4, PLAN-V2 §3.1 y §3.2).

Router registrado desde la fase 0 (`vms/api/routes/__init__.py`); dueño: B5. Todas las rutas son de
administrador (rol A):

  GET  /api/vendors                               → [DriverPublic] (marcas, puertos, avisos, madurez, ejemplos)
  GET  /api/vendors/{id}                          → DriverPublic
  GET  /api/vendors/{id}/paths?channel=&kind=     → rutas RTSP del preset para ese canal (la interfaz no las
                                                    repite en JS)
  POST /api/devices/{id}/identity                 → lee serie y MAC del equipo y las guarda (DeviceIdentity)
  POST /api/devices/{id}/codec-fix                → «Corregir códec» (exige confirm=true; copia + auditoría)
  GET  /api/devices/{id}/codec-fix                → cambios de los últimos 30 días (para «Deshacer»)
  POST /api/devices/{id}/codec-fix/undo           → repone la copia
  POST /api/devices/ip-check                      → busca en la red equipos que cambiaron de IP (serie/MAC);
                                                    aplica sola la IP nueva si el equipo tiene follow_ip, no da
                                                    vídeo y la API autenticada confirma que es el mismo equipo
  POST /api/devices/{id}/move                     → acepta una propuesta reciente de cambio de IP; sin
                                                    `password`, solo si la API confirma la identidad
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel, Field, SecretStr
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import NotFoundError, ValidationFailed
from vms.core.interfaces import Capability, PathStatus
from vms.core.models import AppConfig, Device, DeviceIdentity, DeviceKind
from vms.core.naming import mtx_path
from vms.vendors import registry
from vms.vendors.codecfix import CodecFixer
from vms.vendors.ipwatch import IpChangeProposal, apply_move, cannot_verify, find_moves, verify_at_new_host

from ..deps import Principal, get_state, require_admin
from ..errors import json_response
from ..security import client_ip
from .devices import info_changes
from ..state import AppState

log = logging.getLogger("vms.api.vendors")


@asynccontextmanager
async def ip_watch_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranca con el backend la vigilancia de «seguir la IP» (`ip_watch_loop`) y la cancela al parar."""
    task = asyncio.create_task(ip_watch_loop(app.state.vms), name="ip-watch")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


router = APIRouter(prefix="/api", tags=["vendors"], lifespan=ip_watch_lifespan)

PROPOSAL_TTL_S = 600.0
_proposals: dict[int, tuple[float, list[IpChangeProposal]]] = {}   # id(AppState) → (hora, [IpChangeProposal])


def _device(cfg: AppConfig, device_id: str) -> Device:
    dev = cfg.device(device_id)
    if dev is None:
        raise NotFoundError("El equipo no existe")
    return dev


# --------------------------------------------------------------------------- registro
@router.get("/vendors")
async def list_vendors(_: Principal = Depends(require_admin)) -> Response:
    return json_response(registry.public_vendors())


@router.get("/vendors/{vendor_id}")
async def get_vendor(vendor_id: str, _: Principal = Depends(require_admin)) -> Response:
    spec = registry.get_driver(vendor_id)
    if spec is None:
        raise NotFoundError("Esa marca no está disponible en esta versión")
    return json_response(spec.public())


@router.get("/vendors/{vendor_id}/paths")
async def vendor_paths(vendor_id: str, channel: int = Query(1, ge=1, le=512),
                       kind: DeviceKind = Query("camera"), _: Principal = Depends(require_admin)) -> Response:
    reg = registry
    spec = reg.get_driver(vendor_id)
    if spec is None:
        raise NotFoundError("Esa marca no está disponible en esta versión")
    preset = reg.preset_for(vendor_id, channel, kind)
    if preset is None:
        return json_response({"vendor": vendor_id, "channel": channel, "manual": spec.client is None,
                              "main": None, "sub": None, "variants": []})
    return json_response({"vendor": vendor_id, "channel": channel, "manual": False, "main": preset.main,
                          "sub": preset.sub, "rtsp_port": preset.rtsp_port, "query_safe": preset.query_safe,
                          "variants": [v.main for v in reg.variants_for(vendor_id, channel, kind)]})


# --------------------------------------------------------------------------- identidad
@router.post("/devices/{device_id}/identity")
async def refresh_identity(device_id: str, p: Principal = Depends(require_admin),
                           state: AppState = Depends(get_state)) -> Response:
    dev = _device(state.config(), device_id)
    client = state.client_factory(dev, state.creds.get_device_password(device_id))
    try:
        info = await client.probe()
    finally:
        await client.aclose()
    ident = DeviceIdentity(serial=info.serial[:64], mac=info.mac.lower()[:32], source="api",
                           seen_at=datetime.now(timezone.utc))

    def mutate(cfg: AppConfig) -> Device:
        cur = _device(cfg, device_id)
        data = cur.model_dump()
        data.update(info_changes(info))
        data["identity"] = ident.model_dump()
        updated = Device.model_validate(data)
        cfg.devices = [updated if d.id == device_id else d for d in cfg.devices]
        return updated

    updated = await state.update_config(mutate, "devices")
    log.info("Identidad del equipo «%s» leída por «%s»", updated.name, p.username)
    return json_response(updated.identity)


# --------------------------------------------------------------------------- «Corregir códec»
class CodecFixRequest(BaseModel):
    channel: int = Field(ge=1, le=512)
    stream: Literal["main", "sub"] = "sub"
    codec: Literal["H.264"] = "H.264"
    confirm: bool = False


class CodecUndoRequest(BaseModel):
    backup_id: str = Field(min_length=8, max_length=64)


def _fixer(state: AppState) -> CodecFixer:
    return CodecFixer(state.paths.config_dir)


def _require_codec_fix(dev: Device) -> None:
    spec = registry.get_driver(dev.vendor)
    if spec is None or Capability.API_CODEC_FIX not in spec.capabilities:
        raise ValidationFailed("Esta marca no permite cambiar el códec desde el programa: hazlo en el equipo")


@router.post("/devices/{device_id}/codec-fix")
async def codec_fix(device_id: str, body: CodecFixRequest, request: Request, p: Principal = Depends(require_admin),
                    state: AppState = Depends(get_state)) -> Response:
    if not body.confirm:
        msg = "Confirma el cambio: se modificará la configuración del equipo (se guarda una copia para deshacerlo)"
        raise ValidationFailed(msg, details={"fields": [{"loc": ["confirm"], "msg": msg}]})
    dev = _device(state.config(), device_id)
    _require_codec_fix(dev)
    client = state.client_factory(dev, state.creds.get_device_password(device_id))
    try:
        record = await _fixer(state).apply(device_id, client, body.channel, user=p.username,
                                           ip=client_ip(request), stream=body.stream, codec=body.codec)
    finally:
        await client.aclose()
    return json_response(record, 201)


@router.get("/devices/{device_id}/codec-fix")
async def codec_fix_history(device_id: str, _: Principal = Depends(require_admin),
                            state: AppState = Depends(get_state)) -> Response:
    _device(state.config(), device_id)
    return json_response(_fixer(state).history(device_id))


@router.post("/devices/{device_id}/codec-fix/undo")
async def codec_fix_undo(device_id: str, body: CodecUndoRequest, request: Request,
                         p: Principal = Depends(require_admin), state: AppState = Depends(get_state)) -> Response:
    dev = _device(state.config(), device_id)
    _require_codec_fix(dev)
    client = state.client_factory(dev, state.creds.get_device_password(device_id))
    try:
        record = await _fixer(state).undo(device_id, client, body.backup_id, user=p.username, ip=client_ip(request))
    finally:
        await client.aclose()
    return json_response(record)


# --------------------------------------------------------------------------- cambio de IP
class IpCheckRequest(BaseModel):
    timeout_s: float = Field(3.0, ge=1, le=10)


class MoveRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    password: SecretStr | None = Field(None, max_length=256)


async def run_ip_check(state: AppState, timeout: float = 3.0, *, user: str = "sistema",
                       only: set[str] | None = None) -> list[IpChangeProposal]:
    """Descubre y propone; aplica sola la IP nueva de los equipos con `follow_ip` que **no dan vídeo** y cuya
    identidad se comprueba en la IP nueva con la API autenticada (ver `vms.vendors.ipwatch`). La usan la ruta
    y el bucle en segundo plano (`only` = equipos sin vídeo)."""
    found = await state.discoverer(timeout)
    cfg = state.config()
    proposals = find_moves(cfg.devices, found, only=only)
    stale = only if only is not None else await devices_without_video(state)
    now = datetime.now(timezone.utc)
    auto: list[IpChangeProposal] = []
    for pr in proposals:
        dev = cfg.device(pr.device_id)
        if dev is None:
            continue
        if not pr.follow_ip or pr.device_id not in stale:
            # solo propuesta: no se contacta con la IP nueva hasta que el administrador la acepte
            pr.needs_password = cannot_verify(apply_move(dev, pr, now)) is not None
            continue
        reason = await _check_move(state, dev, pr, now)
        if reason is None:
            auto.append(pr)
        else:
            pr.needs_password = True
            pr.message_es += f" No se ha cambiado sola: {reason}."
    if auto:
        def mutate(c: AppConfig) -> None:
            for pr in auto:
                cur = c.device(pr.device_id)
                if cur is not None and cur.host == pr.old_host \
                        and not any(d.id != cur.id and d.host.lower() == pr.new_host.lower() for d in c.devices):
                    moved = apply_move(cur, pr, now)
                    c.devices = [moved if d.id == cur.id else d for d in c.devices]
                    pr.applied = True

        await state.update_config(mutate, "devices")
        for pr in auto:
            if pr.applied:
                audit("device_moved", user=user, ip="", device_id=pr.device_id, old_host=pr.old_host,
                      new_host=pr.new_host, match=pr.match, auto=True, verified=True)
                log.warning("Equipo «%s» seguido de %s a %s (misma %s, comprobada con la API) por «%s»",
                            pr.device_name, pr.old_host, pr.new_host, "serie" if pr.match == "serial" else "MAC", user)
        if any(pr.applied for pr in auto):
            state.schedule_apply()
    _proposals[id(state)] = (time.monotonic(), list(proposals))
    return list(proposals)


async def _check_move(state: AppState, dev: Device, pr: IpChangeProposal, now: datetime) -> str | None:
    """None si el equipo de la IP nueva es el mismo (API autenticada); si no, el motivo en español."""
    reason = await verify_at_new_host(apply_move(dev, pr, now), state.creds.get_device_password(dev.id),
                                      state.client_factory)
    pr.verified = reason is None
    return reason


async def devices_without_video(state: AppState) -> set[str]:
    """Equipos con «seguir la IP» cuyo flujo principal no llega (candidatos a haber cambiado de IP)."""
    cfg = state.config()
    follow = {d.id for d in cfg.devices if d.follow_ip and d.enabled}
    if not follow:
        return set()
    paths = await state.paths_status_safe()
    if paths is None:
        return set()
    out: set[str] = set()
    for dev_id in follow:
        cams = [c for c in cfg.cameras_of(dev_id) if c.enabled]
        if cams and not any((paths.get(mtx_path(c.id, "main")) or PathStatus(name="")).ready for c in cams):
            out.add(dev_id)
    return out


async def ip_watch_loop(state: AppState, interval: float = 120.0, timeout: float = 3.0) -> None:
    """Bucle para el lifespan del backend: si un equipo con «seguir la IP» se queda sin vídeo, busca en la red
    y aplica la IP nueva si la serie o la MAC coinciden (PLAN-V2 §3.2 punto 11). Nunca lanza."""
    while True:
        await asyncio.sleep(interval)
        try:
            stale = await devices_without_video(state)
            if stale:
                await run_ip_check(state, timeout, only=stale)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - la vigilancia no puede tumbar el backend
            log.exception("Error buscando equipos que cambiaron de IP")


@router.post("/devices/ip-check")
async def ip_check(body: IpCheckRequest | None = None, p: Principal = Depends(require_admin),
                   state: AppState = Depends(get_state)) -> Response:
    proposals = await run_ip_check(state, (body or IpCheckRequest()).timeout_s, user=p.username)
    return json_response({"proposals": proposals})


@router.post("/devices/{device_id}/move")
async def move_device(device_id: str, body: MoveRequest, request: Request, p: Principal = Depends(require_admin),
                      state: AppState = Depends(get_state)) -> Response:
    stamp, items = _proposals.get(id(state), (0.0, []))
    fresh = time.monotonic() - stamp < PROPOSAL_TTL_S
    proposal = next((pr for pr in items if pr.device_id == device_id
                     and pr.new_host.lower() == body.host.strip().lower()), None)
    if proposal is None or not fresh:
        raise ValidationFailed("No hay una propuesta reciente para esa IP: vuelve a buscar en la red "
                               "(«Buscar cambios de IP») y acepta la propuesta.")
    now = datetime.now(timezone.utc)
    password = body.password.get_secret_value() if body.password is not None else None
    if password is None:
        # La contraseña guardada solo va a la IP nueva si la API autenticada confirma que es el mismo equipo
        # (misma regla que PATCH /api/devices: no se manda a otra dirección sin que el administrador lo decida).
        reason = await _check_move(state, _device(state.config(), device_id), proposal, now)
        if reason is not None:
            msg = f"Vuelve a escribir la contraseña del equipo para usar {proposal.new_host}: {reason}"
            raise ValidationFailed(msg, details={"fields": [{"loc": ["password"], "msg": msg}]})

    def mutate(cfg: AppConfig) -> Device:
        cur = _device(cfg, device_id)
        if cur.host != proposal.old_host:
            raise ValidationFailed("El equipo ya cambió de dirección; vuelve a buscar")
        if any(d.id != device_id and d.host.lower() == proposal.new_host.lower() for d in cfg.devices):
            raise ValidationFailed(f"Ya hay otro equipo en {proposal.new_host}")
        moved = apply_move(cur, proposal, now)
        cfg.devices = [moved if d.id == device_id else d for d in cfg.devices]
        return moved

    moved = await state.update_config(mutate, "devices")
    if password is not None:
        if password:
            state.creds.set_device_password(device_id, password)
        else:
            state.creds.delete_device_password(device_id)
    audit("device_moved", user=p.username, ip=client_ip(request), device_id=device_id, old_host=proposal.old_host,
          new_host=proposal.new_host, match=proposal.match, auto=False, verified=password is None,
          password_retyped=password is not None)
    log.info("Equipo «%s» movido de %s a %s por «%s»", moved.name, proposal.old_host, proposal.new_host, p.username)
    state.schedule_apply()
    return json_response({"device_id": device_id, "host": moved.host})


__all__ = ["devices_without_video", "ip_watch_loop", "router", "run_ip_check"]
