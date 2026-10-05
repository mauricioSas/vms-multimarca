"""«¿Por qué no conecta?» (CONTRATO §18.11). Dueño: B6.

`POST /api/diagnostics/device` (A) `{"device_id"}` o un `DeviceTestRequest` (equipo aún sin guardar) →
`DiagnosisResult`; `POST /api/diagnostics/camera/{camera_id}` (A). Reglas deterministas con UN solo intento
con credenciales (respeta el bloqueo de usuario de cada marca).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request
from pydantic import ValidationError
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import NotFoundError, ValidationFailed
from vms.core.models import DeviceTestRequest, known_fields_only
from vms.ops.diagnose import DiagnoseInput

from ..deps import Principal, get_state, require_admin
from ..errors import json_response
from ..security import client_ip
from ..state import AppState
from .health import get_ops

if TYPE_CHECKING:
    from vms.ops.service import OpsService

log = logging.getLogger("vms.api.diagnostics")
router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.post("/device")
async def diagnose_device(request: Request, body: dict[str, Any] = Body(...), p: Principal = Depends(require_admin),
                          state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    cfg = state.config()
    if "device_id" in body:
        dev = cfg.device(str(body["device_id"]))
        if dev is None:
            raise NotFoundError("El equipo no existe")
        inp = DiagnoseInput(device=dev, password=state.creds.get_device_password(dev.id), device_id=dev.id,
                            cameras=cfg.cameras_of(dev.id))
    else:
        try:
            req = DeviceTestRequest.model_validate(known_fields_only(DeviceTestRequest, body))
        except ValidationError as exc:
            from ..errors import field_errors
            raise ValidationFailed("Hay datos no válidos", details={"fields": field_errors(list(exc.errors()))}) \
                from exc
        inp = DiagnoseInput(device=req, password=req.password.get_secret_value() if req.password else "")
    result = await ops.diagnose(inp)
    audit("device_diagnosis", user=p.username, ip=client_ip(request), device_id=inp.device_id,
          failed=[s.code for s in result.steps if s.ok is False])
    return json_response(result)


@router.post("/camera/{camera_id}")
async def diagnose_camera(camera_id: str, request: Request, p: Principal = Depends(require_admin),
                          state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    cfg = state.config()
    cam = cfg.camera(camera_id)
    dev = cfg.device(cam.device_id) if cam else None
    if cam is None or dev is None:
        raise NotFoundError("La cámara no existe")
    inp = DiagnoseInput(device=dev, password=state.creds.get_device_password(dev.id), device_id=dev.id,
                        cameras=[cam], camera_id=cam.id)
    result = await ops.diagnose(inp)
    audit("device_diagnosis", user=p.username, ip=client_ip(request), device_id=dev.id, camera_id=cam.id,
          failed=[s.code for s in result.steps if s.ok is False])
    return json_response(result)
