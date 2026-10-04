"""Reglas y ajustes de analítica + configuración interna para el proceso de analítica (CONTRATO §6.10, §8.2)."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import pydantic_core
from fastapi import APIRouter, Body, Depends, Request
from pydantic import TypeAdapter
from starlette.responses import Response

from vms.core.errors import NotFoundError, ValidationFailed
from vms.core.models import AnalyticsRule, AppConfig, CameraAnalytics, LineRule, ZoneRule, known_fields_only

from ..deps import Principal, get_state, require_admin, require_internal, require_operator
from ..errors import json_response
from ..state import AppState
from .cameras import get_camera

log = logging.getLogger("vms.api.analytics")
router = APIRouter(prefix="/api", tags=["analytics"])

_rule_adapter: TypeAdapter[LineRule | ZoneRule] = TypeAdapter(AnalyticsRule)


def _known(body: dict[str, Any]) -> dict[str, Any]:
    """Solo los campos del tipo de regla (las reglas guardadas admiten campos de otras versiones)."""
    model = ZoneRule if body.get("kind") == "zone" else LineRule
    return known_fields_only(model, body)  # type: ignore[no-any-return]


def _get_rule(cfg: AppConfig, rule_id: str) -> LineRule | ZoneRule:
    rule = next((r for r in cfg.analytics_rules if r.id == rule_id), None)
    if rule is None:
        raise NotFoundError("La regla no existe")
    return rule


def _camera_must_exist(cfg: AppConfig, camera_id: str) -> None:
    if cfg.camera(camera_id) is None:
        raise ValidationFailed("La cámara indicada no existe",
                               details={"fields": [{"loc": ["camera_id"], "msg": "La cámara no existe"}]})


def _check_rule(rule: LineRule | ZoneRule) -> None:
    if isinstance(rule, ZoneRule):
        problem = rule.clear_below_problem()
        if problem:
            raise ValidationFailed(problem, details={"fields": [{"loc": ["clear_below"], "msg": problem}]})


@router.get("/analytics/rules")
async def list_rules(camera_id: str | None = None, _: Principal = Depends(require_operator),
                     state: AppState = Depends(get_state)) -> Response:
    rules = state.config().analytics_rules
    if camera_id:
        rules = [r for r in rules if r.camera_id == camera_id]
    return json_response(rules)


@router.post("/analytics/rules")
async def create_rule(body: dict[str, Any] = Body(...), p: Principal = Depends(require_admin),
                      state: AppState = Depends(get_state)) -> Response:
    data = {k: v for k, v in _known(body).items() if k not in ("id", "updated_at")}
    rule = _rule_adapter.validate_python(data)
    _check_rule(rule)

    def mutate(cfg: AppConfig) -> None:
        _camera_must_exist(cfg, rule.camera_id)
        cfg.analytics_rules.append(rule)

    await state.update_config(mutate, "analytics")
    log.info("Regla de analítica «%s» (%s) creada por «%s»", rule.name, rule.kind, p.username)
    return json_response(rule, 201)


@router.get("/analytics/rules/{rule_id}")
async def get_rule(rule_id: str, _: Principal = Depends(require_operator),
                   state: AppState = Depends(get_state)) -> Response:
    return json_response(_get_rule(state.config(), rule_id))


@router.put("/analytics/rules/{rule_id}")
async def put_rule(rule_id: str, body: dict[str, Any] = Body(...), p: Principal = Depends(require_admin),
                   state: AppState = Depends(get_state)) -> Response:
    current = _get_rule(state.config(), rule_id)
    data = {**_known({"kind": current.kind, **body}), "id": rule_id, "updated_at": datetime.now(timezone.utc)}
    data.setdefault("kind", current.kind)
    data.setdefault("camera_id", current.camera_id)
    if data["kind"] != current.kind or data["camera_id"] != current.camera_id:
        raise ValidationFailed("No se puede cambiar el tipo ni la cámara de una regla; crea una nueva",
                               details={"fields": [{"loc": ["kind" if data["kind"] != current.kind else "camera_id"],
                                                    "msg": "No se puede cambiar"}]})
    rule = _rule_adapter.validate_python(data)
    _check_rule(rule)

    def mutate(cfg: AppConfig) -> None:
        _get_rule(cfg, rule_id)
        cfg.analytics_rules = [rule if r.id == rule_id else r for r in cfg.analytics_rules]

    await state.update_config(mutate, "analytics")
    log.info("Regla de analítica «%s» modificada por «%s»", rule.name, p.username)
    return json_response(rule)


@router.delete("/analytics/rules/{rule_id}")
async def delete_rule(rule_id: str, p: Principal = Depends(require_admin),
                      state: AppState = Depends(get_state)) -> Response:
    def mutate(cfg: AppConfig) -> LineRule | ZoneRule:
        rule = _get_rule(cfg, rule_id)
        cfg.analytics_rules = [r for r in cfg.analytics_rules if r.id != rule_id]
        return rule

    rule = await state.update_config(mutate, "analytics")
    log.info("Regla de analítica «%s» borrada por «%s»", rule.name, p.username)
    return Response(status_code=204)


@router.get("/analytics/cameras")
async def list_analytics_cameras(_: Principal = Depends(require_operator),
                                 state: AppState = Depends(get_state)) -> Response:
    # Solo las cámaras con ajustes guardados (las demás usan los valores por defecto de CameraAnalytics)
    return json_response(state.config().analytics_cameras)


@router.put("/analytics/cameras/{camera_id}")
async def put_analytics_camera(camera_id: str, body: dict[str, Any] = Body(...), p: Principal = Depends(require_admin),
                               state: AppState = Depends(get_state)) -> Response:
    get_camera(state.config(), camera_id)
    item = CameraAnalytics.model_validate({**known_fields_only(CameraAnalytics, body), "camera_id": camera_id})

    def mutate(cfg: AppConfig) -> None:
        get_camera(cfg, camera_id)
        cfg.analytics_cameras = [a for a in cfg.analytics_cameras if a.camera_id != camera_id] + [item]

    await state.update_config(mutate, "analytics")
    log.info("Analítica de la cámara %s %s por «%s» (%.1f fps)", camera_id,
             "activada" if item.enabled else "desactivada", p.username, item.fps)
    return json_response(item)


@router.get("/analytics/status")
async def analytics_status(_: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    return json_response(state.analytics_status())


def build_internal_config(state: AppState) -> dict[str, Any]:
    """Documento §8.2 con la implementación única de analytics.config (sin duplicar reglas).

    Antes se quitan las cámaras de equipos deshabilitados: el motor no tiene sus rutas.
    """
    from analytics.config import build_analytics_config

    cfg = state.repo.snapshot()
    cfg.analytics_cameras = [a for a in cfg.analytics_cameras
                             if (cam := cfg.camera(a.camera_id)) is not None
                             and (dev := cfg.device(cam.device_id)) is not None and dev.enabled]
    def rtsp_url(camera_id: str, stream: str) -> str:
        return state.engine.rtsp_read_url(camera_id, "main" if stream == "main" else "sub")

    doc = build_analytics_config(cfg, state.repo.revision, cfg.settings.site.id, rtsp_url)
    return doc.model_dump(mode="json")


@router.get("/internal/analytics/config", dependencies=[Depends(require_internal)])
async def internal_config(request: Request, state: AppState = Depends(get_state)) -> Response:
    data = build_internal_config(state)
    # ETag por contenido (no por revisión): la revisión vuelve a 0 al reiniciar el backend y un
    # ETag «"0"» antiguo podría dar un 304 falso con una configuración distinta.
    content = {k: v for k, v in data.items() if k != "revision"}
    etag = '"' + hashlib.sha256(pydantic_core.to_json(content)).hexdigest()[:20] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return json_response(data, headers={"ETag": etag, "Cache-Control": "no-cache"})
