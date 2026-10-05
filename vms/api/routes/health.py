"""Salud de imagen (0-100), informe de salud, desfase horario y previsión de días de grabación.

Contrato: CONTRATO §18.2-§18.5. Dueño: B6. El `lifespan` de este router arranca el servicio de B6
(`vms.ops.service.OpsService`) y lo deja en `app.state.ops`; FastAPI lo encadena al de la aplicación, así
no hace falta tocar `vms/api/app.py`.

| Método y ruta | Rol |
|---|---|
| `GET /api/camera-health` · `GET /api/camera-health/{id}` | O (respeta el ámbito por cámara) |
| `POST /api/camera-health/{id}/reference` `{"kind": "day"\\|"night"}` → 202 `{"job_id"}` | A |
| `GET /api/camera-health/jobs/{job_id}` (progreso de «Fijar referencia») | A |
| `GET /api/camera-health/{id}/reference.jpg?kind=day` (queda en audit.log) | A |
| `GET /api/camera-health/{id}/now.jpg` (en memoria, no se guarda) | A |
| `PUT /api/camera-health/{id}/config` | A |
| `POST /api/camera-health/{id}/check` | A |
| `GET /api/clock` · `POST /api/clock/check` | O · A |
| `GET /api/clock/settings` · `PUT /api/clock/settings` (`pc_sntp_enabled`) | A |
| `GET /api/retention-forecast` · `POST /api/retention-forecast/simulate` | O · A |
| `GET /api/health-report?date=` · `GET /api/health-report.csv?date=` | O |
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import NotFoundError, ValidationFailed
from vms.core.models import AppConfig, CameraHealthConfig, known_fields_only

from ..deps import Principal, get_state, require_admin, require_operator
from ..errors import json_response
from ..permissions import ensure_camera_access, visible_camera_ids
from ..security import client_ip
from ..state import AppState

log = logging.getLogger("vms.api.health")

if TYPE_CHECKING:
    from vms.ops.service import OpsService


@asynccontextmanager
async def ops_lifespan(app: FastAPI) -> AsyncIterator[None]:
    from vms.ops.service import OpsService

    state: AppState = app.state.vms
    svc = OpsService(state)
    app.state.ops = svc
    await svc.start()
    try:
        yield
    finally:
        await svc.stop()
        if getattr(app.state, "ops", None) is svc:
            del app.state.ops


router = APIRouter(prefix="/api", tags=["health"], lifespan=ops_lifespan)


def get_ops(conn: HTTPConnection) -> "OpsService":
    """El servicio de B6. Sin `lifespan` (algunas pruebas) se crea sin tareas en segundo plano."""
    svc = getattr(conn.app.state, "ops", None)
    if svc is None:
        from vms.ops.service import OpsService
        svc = OpsService(conn.app.state.vms)
        conn.app.state.ops = svc
    return svc  # type: ignore[no-any-return]


def _camera_or_404(state: AppState, p: Principal, camera_id: str) -> None:
    ensure_camera_access(state, p, camera_id, "live")
    if state.config().camera(camera_id) is None:
        raise NotFoundError("La cámara no existe")


# --------------------------------------------------------------------------- salud de imagen
@router.get("/camera-health")
async def list_health(p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                      ops: "OpsService" = Depends(get_ops)) -> Response:
    cfg = state.config()
    visible = visible_camera_ids(state, p, (c.id for c in cfg.cameras), "live")
    return json_response(ops.all_health([c.id for c in cfg.cameras if c.id in visible]))


@router.get("/camera-health/jobs/{job_id}")
async def reference_job(job_id: str, _: Principal = Depends(require_admin),
                        ops: "OpsService" = Depends(get_ops)) -> Response:
    job = ops.jobs.get(job_id)
    if job is None:
        raise NotFoundError("No existe ese trabajo")
    return json_response(job)


@router.get("/camera-health/{camera_id}")
async def get_health(camera_id: str, p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                     ops: "OpsService" = Depends(get_ops)) -> Response:
    _camera_or_404(state, p, camera_id)
    return json_response(ops.camera_health(camera_id))


class ReferenceRequest(BaseModel):
    kind: Literal["day", "night"] = "day"


@router.post("/camera-health/{camera_id}/reference")
async def set_reference(camera_id: str, request: Request, body: ReferenceRequest = Body(default=ReferenceRequest()),
                        p: Principal = Depends(require_admin), state: AppState = Depends(get_state),
                        ops: "OpsService" = Depends(get_ops)) -> Response:
    _camera_or_404(state, p, camera_id)
    job_id = ops.start_reference_job(camera_id, body.kind, p.username)
    audit("health_reference_request", user=p.username, ip=client_ip(request), camera_id=camera_id, kind=body.kind)
    return json_response({"job_id": job_id}, 202)


@router.get("/camera-health/{camera_id}/reference.jpg")
async def reference_jpg(camera_id: str, request: Request, kind: Literal["day", "night"] = "day",
                        p: Principal = Depends(require_admin), state: AppState = Depends(get_state),
                        ops: "OpsService" = Depends(get_ops)) -> Response:
    _camera_or_404(state, p, camera_id)
    data = ops.refs.jpeg(camera_id, kind)
    if data is None:
        raise NotFoundError("Esta cámara aún no tiene imagen de referencia")
    audit("health_reference_view", user=p.username, ip=client_ip(request), camera_id=camera_id, kind=kind)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/camera-health/{camera_id}/now.jpg")
async def now_jpg(camera_id: str, request: Request, p: Principal = Depends(require_admin),
                  state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    _camera_or_404(state, p, camera_id)
    data = await ops.current_jpeg(camera_id)
    audit("snapshot", user=p.username, ip=client_ip(request), camera_id=camera_id, purpose="salud: antes/ahora")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/camera-health/{camera_id}/config")
async def get_health_config(camera_id: str, p: Principal = Depends(require_admin),
                            state: AppState = Depends(get_state)) -> Response:
    _camera_or_404(state, p, camera_id)
    cam = state.config().camera(camera_id)
    assert cam is not None
    return json_response(cam.health)


@router.put("/camera-health/{camera_id}/config")
async def put_health_config(camera_id: str, body: dict[str, Any] = Body(...), p: Principal = Depends(require_admin),
                            state: AppState = Depends(get_state)) -> Response:
    _camera_or_404(state, p, camera_id)
    new = CameraHealthConfig.model_validate(known_fields_only(CameraHealthConfig, body))
    for poly in new.masks:
        if len(poly) < 3 or any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in poly):
            raise ValidationFailed("Cada zona excluida necesita al menos 3 puntos entre 0 y 1")

    def mutate(cfg: AppConfig) -> CameraHealthConfig:
        cam = cfg.camera(camera_id)
        if cam is None:
            raise NotFoundError("La cámara no existe")
        cam.health = new
        cam.updated_at = datetime.now(timezone.utc)
        return new

    out = await state.update_config(mutate, "cameras")
    log.info("Ajustes de salud de %s cambiados por «%s»", camera_id, p.username)
    return json_response(out)


@router.post("/camera-health/{camera_id}/check")
async def check_now(camera_id: str, p: Principal = Depends(require_admin), state: AppState = Depends(get_state),
                    ops: "OpsService" = Depends(get_ops)) -> Response:
    _camera_or_404(state, p, camera_id)
    return json_response(await ops.check_camera(camera_id))


# --------------------------------------------------------------------------- hora
@router.get("/clock")
async def get_clock(p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                    ops: "OpsService" = Depends(get_ops)) -> Response:
    latest = ops.store.latest_clock_checks()
    cfg = state.config()
    visible = visible_camera_ids(state, p, (c.id for c in cfg.cameras), "live")
    devices = [chk for did, chk in latest.items() if did is not None and cfg.device(did) is not None
               and (p.role == "admin" or any(c.id in visible for c in cfg.cameras_of(did)))]
    return json_response({"pc": latest.get(None), "devices": devices})


@router.post("/clock/check")
async def clock_check(_: Principal = Depends(require_admin), ops: "OpsService" = Depends(get_ops)) -> Response:
    pc, devices = await ops.clock_check_all()
    return json_response({"pc": pc, "devices": devices})


class ClockSettings(BaseModel):
    pc_sntp_enabled: bool = Field(description="Comprobar la hora del PC con su servidor de hora (UDP 123)")


@router.get("/clock/settings")
async def get_clock_settings(_: Principal = Depends(require_admin), ops: "OpsService" = Depends(get_ops)) -> Response:
    return json_response({"pc_sntp_enabled": ops.pc_sntp_enabled()})


@router.put("/clock/settings")
async def put_clock_settings(body: ClockSettings, request: Request, p: Principal = Depends(require_admin),
                             ops: "OpsService" = Depends(get_ops)) -> Response:
    ops.set_pc_sntp_enabled(body.pc_sntp_enabled)
    audit("clock_settings", user=p.username, ip=client_ip(request), pc_sntp_enabled=body.pc_sntp_enabled)
    return json_response({"pc_sntp_enabled": body.pc_sntp_enabled})


# --------------------------------------------------------------------------- previsión
@router.get("/retention-forecast")
async def get_forecast(p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                       ops: "OpsService" = Depends(get_ops)) -> Response:
    fc = await asyncio.to_thread(ops.forecast)
    if p.role != "admin":
        # el detalle por cámara solo de las que ve (ámbito por cámara: no se revela que existen las demás); los
        # totales del disco son de la tienda y se dejan
        visible = visible_camera_ids(state, p, (c.camera_id for c in fc.cameras), "live")
        fc = fc.model_copy(update={"cameras": [c for c in fc.cameras if c.camera_id in visible]})
    return json_response(fc)


class SimulateRequest(BaseModel):
    add_cameras: int = Field(0, ge=0, le=256)
    bitrate_mbps: float = Field(4.0, ge=0.1, le=100)


@router.post("/retention-forecast/simulate")
async def simulate(body: SimulateRequest, _: Principal = Depends(require_admin),
                   ops: "OpsService" = Depends(get_ops)) -> Response:
    return json_response(await asyncio.to_thread(ops.forecast, body.add_cameras, body.bitrate_mbps))


# --------------------------------------------------------------------------- informe
def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationFailed("Fecha no válida (usa AAAA-MM-DD)",
                               details={"fields": [{"loc": ["date"], "msg": "Fecha no válida"}]}) from exc


def _scoped_report(report: Any, state: AppState, p: Principal) -> Any:
    if p.role == "admin":
        return report
    visible = visible_camera_ids(state, p, (c.camera_id for c in report.cameras), "live")
    hidden = {c.name for c in report.cameras if c.camera_id not in visible}
    return report.model_copy(update={
        "cameras": [c for c in report.cameras if c.camera_id in visible],
        "problems": [x for x in report.problems if not any(x.startswith(f"{n}:") for n in hidden)]})


@router.get("/health-report")
async def health_report(date: str | None = None, p: Principal = Depends(require_operator),
                        state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    return json_response(_scoped_report(await ops.report(_parse_day(date)), state, p))


@router.get("/health-report.csv")
async def health_report_csv(date: str | None = None, p: Principal = Depends(require_operator),
                            state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    from vms.ops.report import report_csv
    report = _scoped_report(await ops.report(_parse_day(date)), state, p)
    return Response(report_csv(report), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="salud_{report.site_id}_{report.date}.csv"',
        "Cache-Control": "no-store"})
