"""Equipos (cámaras IP y NVR), importación de canales y descubrimiento (CONTRATO §6.3)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from starlette.responses import Response

from vms.core.errors import ConflictError, DeviceError, NotFoundError, ValidationFailed
from vms.core.interfaces import ChannelInfo, DeviceInfo
from vms.core.models import AppConfig, Camera, Device, DeviceCreate, DeviceTestRequest, DeviceUpdate
from vms.vendors.localnet import local_ipv4, subnet_hint

from ..deps import Principal, get_state, require_admin, require_operator
from ..errors import json_response
from ..state import AppState
from ..views import device_out

log = logging.getLogger("vms.api.devices")
router = APIRouter(tags=["devices"])

DEVICE_FIELDS = set(DeviceUpdate.model_fields) - {"password"}
# Si cambia alguno de estos, la contraseña guardada no se reutiliza (iría a otro equipo).
ADDRESS_FIELDS = ("host", "http_port", "rtsp_port", "onvif_port", "https")


class ImportRequest(BaseModel):
    channels: list[int] | Literal["all"] = "all"


class ScanRequest(BaseModel):
    timeout_s: float = Field(3.0, ge=1, le=10)


def _get_device(cfg: AppConfig, device_id: str) -> Device:
    dev = cfg.device(device_id)
    if dev is None:
        raise NotFoundError("El equipo no existe")
    return dev


def _check_duplicate(cfg: AppConfig, dev: Device) -> None:
    for other in cfg.devices:
        if (other.id != dev.id and other.host.lower() == dev.host.lower() and other.http_port == dev.http_port
                and other.rtsp_port == dev.rtsp_port):
            raise ConflictError(f"Ya existe un equipo con la dirección {dev.host} («{other.name}»)")


def _camera_name(dev: Device, ch: ChannelInfo, multi: bool) -> str:
    name = (ch.name or "").strip()
    if not name or (not multi and name.lower() in ("camera 01", "ipcamera", "canal 1")):
        name = dev.name if not multi else f"{dev.name} - Canal {ch.channel}"
    return name[:80]


def cameras_from_channels(dev: Device, channels: list[ChannelInfo], wanted: list[int] | Literal["all"],
                          existing: set[int]) -> list[Camera]:
    want = None if wanted == "all" else set(wanted)
    multi = len(channels) > 1 or dev.kind == "nvr"
    out: list[Camera] = []
    for ch in channels:
        if (want is not None and ch.channel not in want) or ch.channel in existing:
            continue
        out.append(Camera(
            name=_camera_name(dev, ch, multi), device_id=dev.id, channel=ch.channel,
            has_sub=ch.has_sub,
            main_path=ch.main_path if dev.vendor in ("onvif", "generic") else None,
            sub_path=ch.sub_path if dev.vendor in ("onvif", "generic") and ch.has_sub else None))
    if want is not None:
        known = {c.channel for c in channels}
        missing = sorted(want - known)
        if missing:
            raise ValidationFailed(f"El equipo no tiene los canales {', '.join(map(str, missing))}",
                                   details={"missing_channels": missing})
    return out


def info_changes(info: DeviceInfo) -> dict[str, str]:
    """Campos del equipo que se guardan tras leerlo: modelo, serie, firmware **con su fecha de build** (Hikvision la
    da aparte: sin ella la auditoría no puede comparar con los avisos) y el fabricante que dice el equipo (ONVIF).

    La fecha va siempre con el firmware: si cambia el firmware y el equipo no da fecha, se borra la anterior."""
    out = {k: v for k, v in {"model": info.model, "serial": info.serial, "firmware": info.firmware,
                             "manufacturer": info.manufacturer[:64]}.items() if v}
    if info.firmware:
        out["firmware_date"] = info.firmware_date[:32]
    return out


async def _fetch(state: AppState, dev: Device, password: str, *, probe: bool) -> tuple[dict[str, str], list[ChannelInfo]]:
    """(cambios de modelo/serie/firmware, canales) consultando el equipo."""
    client = state.client_factory(dev, password)
    try:
        changes: dict[str, str] = {}
        if probe:
            changes = info_changes(await client.probe())
        return changes, await client.list_channels()
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            log.debug("Error cerrando el cliente del equipo", exc_info=True)


@router.get("/api/devices")
async def list_devices(_: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    cfg = state.config()
    paths = await state.paths_status_safe()
    return json_response([device_out(cfg, d, state.creds, paths) for d in cfg.devices])


@router.post("/api/devices")
async def create_device(body: DeviceCreate, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state)) -> Response:
    dev = Device(**body.model_dump(exclude={"password", "import_channels"}))
    _check_duplicate(state.config(), dev)
    password = body.password.get_secret_value() if body.password else ""
    new_cams: list[Camera] = []
    details: dict[str, object] = {}
    if body.import_channels is not None:
        try:
            changes, channels = await _fetch(state, dev, password, probe=True)
            dev = dev.model_copy(update=changes)
            new_cams = cameras_from_channels(dev, channels, body.import_channels, set())
        except (DeviceError, ValidationFailed) as exc:
            details["import_error"] = exc.message
            log.warning("Equipo «%s» creado sin importar canales: %s", dev.name, exc.message)
    if password:
        state.creds.set_device_password(dev.id, password)

    def mutate(cfg: AppConfig) -> None:
        _check_duplicate(cfg, dev)
        cfg.devices.append(dev)
        cfg.cameras.extend(new_cams)

    try:
        await state.update_config(mutate, "devices")
    except Exception:
        if password:
            state.creds.delete_device_password(dev.id)
        raise
    if new_cams:
        state.publish_config("cameras")
    log.info("Equipo «%s» (%s, %s) creado por «%s» con %d cámaras", dev.name, dev.vendor, dev.host, p.username,
             len(new_cams))
    paths = await state.paths_status_safe()
    return json_response(device_out(state.config(), dev, state.creds, paths, details or None), 201)


@router.post("/api/devices/test")
async def test_new_device(body: DeviceTestRequest, _: Principal = Depends(require_admin),
                          state: AppState = Depends(get_state)) -> Response:
    password = body.password.get_secret_value() if body.password else ""
    result = await state.device_tester(body, password)
    return json_response(result)


@router.get("/api/devices/{device_id}")
async def get_device(device_id: str, _: Principal = Depends(require_operator),
                     state: AppState = Depends(get_state)) -> Response:
    cfg = state.config()
    dev = _get_device(cfg, device_id)
    return json_response(device_out(cfg, dev, state.creds, await state.paths_status_safe()))


@router.patch("/api/devices/{device_id}")
async def update_device(device_id: str, body: DeviceUpdate, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state)) -> Response:
    current = _get_device(state.config(), device_id)
    changes = {k: getattr(body, k) for k in body.model_fields_set & DEVICE_FIELDS}
    # Campos no anulables en el modelo persistente: null = sin cambio
    changes = {k: v for k, v in changes.items() if v is not None or k == "onvif_port"}
    def _norm(v: object) -> object:
        return v.lower() if isinstance(v, str) else v

    moved = [k for k in ADDRESS_FIELDS if k in changes and _norm(changes[k]) != _norm(getattr(current, k))]
    if moved and body.password is None and state.creds.has_device_password(device_id):
        # La contraseña guardada pertenece a la dirección anterior: no se envía a otra IP/puerto sin
        # que el administrador la vuelva a escribir (si no, cambiar la IP y pulsar «Probar» la revelaría).
        msg = "Vuelve a escribir la contraseña del equipo al cambiar su dirección o sus puertos"
        raise ValidationFailed(msg, details={"fields": [{"loc": ["password"], "msg": msg}]})

    def mutate(cfg: AppConfig) -> Device:
        dev = _get_device(cfg, device_id)
        updated = Device.model_validate({**dev.model_dump(), **changes, "updated_at": datetime.now(timezone.utc)})
        _check_duplicate(cfg, updated)
        cfg.devices = [updated if d.id == device_id else d for d in cfg.devices]
        return updated

    updated = await state.update_config(mutate, "devices") if changes else _get_device(state.config(), device_id)
    if body.password is not None:
        state.creds.set_device_password(device_id, body.password.get_secret_value())
        state.schedule_apply()  # la contraseña no está en config.json: hay que avisar al motor a mano
        log.info("Contraseña del equipo «%s» %s por «%s»", updated.name,
                 "borrada" if not body.password.get_secret_value() else "cambiada", p.username)
    if changes:
        log.info("Equipo «%s» modificado por «%s» (%s)", updated.name, p.username, ", ".join(sorted(changes)))
    return json_response(device_out(state.config(), updated, state.creds, await state.paths_status_safe()))


@router.delete("/api/devices/{device_id}")
async def delete_device(device_id: str, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state)) -> Response:
    def mutate(cfg: AppConfig) -> tuple[Device, list[str]]:
        dev = _get_device(cfg, device_id)
        return dev, cfg.remove_device(device_id)

    dev, removed = await state.update_config(mutate, "devices")
    if removed:
        state.publish_config("cameras")
    state.creds.delete_device_password(device_id)
    log.info("Equipo «%s» y sus %d cámaras borrados por «%s»", dev.name, len(removed), p.username)
    return Response(status_code=204)


@router.post("/api/devices/{device_id}/test")
async def test_saved_device(device_id: str, _: Principal = Depends(require_admin),
                            state: AppState = Depends(get_state)) -> Response:
    dev = _get_device(state.config(), device_id)
    result = await state.device_tester(dev, state.creds.get_device_password(device_id))
    if result.info is not None and result.auth_ok:
        # «Probar conexión» deja al día modelo, firmware (con su fecha de build) y fabricante: la auditoría de
        # seguridad los necesita y el equipo puede haberse actualizado desde el alta.
        changes = {k: v for k, v in info_changes(result.info).items() if getattr(dev, k, None) != v}
        if changes:
            def mutate(cfg: AppConfig) -> None:
                cur = _get_device(cfg, device_id)
                if (cur.host, cur.http_port) != (dev.host, dev.http_port):
                    return   # se cambió la dirección mientras se probaba: no se mezclan datos de otro equipo
                updated = Device.model_validate({**cur.model_dump(), **changes,
                                                 "updated_at": datetime.now(timezone.utc)})
                cfg.devices = [updated if d.id == device_id else d for d in cfg.devices]

            try:
                await state.update_config(mutate, "devices")
                log.info("Datos del equipo «%s» actualizados al probarlo (%s)", dev.name, ", ".join(sorted(changes)))
            except Exception:  # noqa: BLE001 - la prueba de conexión no falla por esto
                log.warning("No se pudieron guardar los datos leídos del equipo «%s»", dev.name, exc_info=True)
    return json_response(result)


@router.get("/api/devices/{device_id}/channels")
async def device_channels(device_id: str, _: Principal = Depends(require_admin),
                          state: AppState = Depends(get_state)) -> Response:
    dev = _get_device(state.config(), device_id)
    _info, channels = await _fetch(state, dev, state.creds.get_device_password(device_id), probe=False)
    return json_response(channels)


@router.post("/api/devices/{device_id}/channels/import")
async def import_channels(device_id: str, body: ImportRequest, p: Principal = Depends(require_admin),
                          state: AppState = Depends(get_state)) -> Response:
    dev = _get_device(state.config(), device_id)
    _info, channels = await _fetch(state, dev, state.creds.get_device_password(device_id), probe=False)

    def mutate(cfg: AppConfig) -> list[Camera]:
        _get_device(cfg, device_id)
        existing = {c.channel for c in cfg.cameras_of(device_id)}
        new = cameras_from_channels(dev, channels, body.channels, existing)
        cfg.cameras.extend(new)
        return new

    new = await state.update_config(mutate, "cameras")
    log.info("%d cámaras importadas del equipo «%s» por «%s»", len(new), dev.name, p.username)
    return json_response(new, 201)


@router.post("/api/discovery/scan")
async def discovery_scan(body: ScanRequest | None = None, _: Principal = Depends(require_admin),
                         state: AppState = Depends(get_state)) -> Response:
    timeout = (body or ScanRequest()).timeout_s
    found = await state.discoverer(timeout)
    hosts = {d.host.lower() for d in state.config().devices}
    nets = local_ipv4() if found else []
    out = [d.model_copy(update={"already_added": d.host.lower() in hosts,
                                "network_hint": subnet_hint(d.host, nets)}) for d in found]
    return json_response({"devices": out})
