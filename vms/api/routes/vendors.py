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
                                                    aplica sola la IP nueva si el equipo tiene follow_ip
  POST /api/devices/{id}/move                     → acepta una propuesta reciente de cambio de IP
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import NotFoundError, ValidationFailed
from vms.core.interfaces import Capability
from vms.core.models import AppConfig, Device, DeviceIdentity, DeviceKind
from vms.vendors import registry
from vms.vendors.codecfix import CodecFixer
from vms.vendors.ipwatch import IpChangeProposal, apply_move, find_moves

from ..deps import Principal, get_state, require_admin
from ..errors import json_response
from ..security import client_ip
from ..state import AppState

log = logging.getLogger("vms.api.vendors")
router = APIRouter(prefix="/api", tags=["vendors"])

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
        data.update({k: v for k, v in {"model": info.model, "serial": info.serial, "firmware": info.firmware}.items()
                     if v})
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


async def run_ip_check(state: AppState, timeout: float = 3.0, *, user: str = "sistema") -> list[IpChangeProposal]:
    """Descubre, propone y aplica las de los equipos con `follow_ip`. La usa la ruta y puede usarla un bucle
    en segundo plano del backend (petición al arquitecto: arrancarlo en el lifespan)."""
    found = await state.discoverer(timeout)
    cfg = state.config()
    proposals = find_moves(cfg.devices, found)
    auto = [pr for pr in proposals if pr.follow_ip]
    if auto:
        now = datetime.now(timezone.utc)

        def mutate(c: AppConfig) -> None:
            for pr in auto:
                cur = c.device(pr.device_id)
                if cur is not None and cur.host == pr.old_host:
                    moved = apply_move(cur, pr, now)
                    c.devices = [moved if d.id == cur.id else d for d in c.devices]
                    pr.applied = True

        await state.update_config(mutate, "devices")
        for pr in auto:
            if pr.applied:
                log.warning("Equipo «%s» seguido de %s a %s (misma %s) por «%s»", pr.device_name, pr.old_host,
                            pr.new_host, "serie" if pr.match == "serial" else "MAC", user)
    _proposals[id(state)] = (time.monotonic(), list(proposals))
    return list(proposals)


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
    audit("device_moved", user=p.username, ip=client_ip(request), device_id=device_id,
          old_host=proposal.old_host, new_host=proposal.new_host, match=proposal.match)
    log.info("Equipo «%s» movido de %s a %s por «%s»", moved.name, proposal.old_host, proposal.new_host, p.username)
    state.schedule_apply()
    return json_response({"device_id": device_id, "host": moved.host})


__all__ = ["router", "run_ip_check"]
