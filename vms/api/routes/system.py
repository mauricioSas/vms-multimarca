"""Salud, estado y ajustes del sistema (CONTRATO §6.8 y §6.9)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends
from starlette.responses import Response

from vms import __version__
from vms.core.errors import ValidationFailed
from vms.core.models import AppConfig, RetentionSettings, SystemSettings, known_fields_only

from ..deps import Principal, get_state, require_admin, require_operator
from ..errors import json_response
from ..state import AppState

log = logging.getLogger("vms.api.system")
router = APIRouter(prefix="/api", tags=["system"])


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@router.get("/health")
async def health(state: AppState = Depends(get_state)) -> Response:
    ov = await state.overview()
    engine = ov["engine"]
    return json_response({"status": ov["status"], "version": __version__, "uptime_s": state.uptime_s(),
                          "engine": {"running": engine.running, "api_ok": engine.api_ok}})


@router.get("/status")
async def status(_: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    ov = await state.overview()
    return json_response({
        "status": ov["status"], "problems": ov["problems"], "version": __version__, "uptime_s": state.uptime_s(),
        "engine": ov["engine"], "disk": ov["disk"], "cameras": ov["cameras"], "analytics": ov["analytics"],
        "credential_backend": state.creds.backend_name, "config_warning": state.repo.load_warning,
    })


@router.get("/settings")
async def get_settings(_: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    return json_response(state.config().settings)


@router.patch("/settings")
async def patch_settings(body: dict[str, Any] = Body(...), p: Principal = Depends(require_admin),
                         state: AppState = Depends(get_state)) -> Response:
    unknown = set(body) - set(SystemSettings.model_fields)
    if unknown:
        raise ValidationFailed("Hay ajustes desconocidos",
                               details={"fields": [{"loc": [k], "msg": "Ajuste desconocido"} for k in sorted(unknown)]})

    def mutate(cfg: AppConfig) -> SystemSettings:
        # lo guardado conserva campos de otras versiones; el cuerpo solo aporta campos conocidos
        merged = deep_merge(cfg.settings.model_dump(mode="json"), known_fields_only(SystemSettings, body))
        cfg.settings = SystemSettings.model_validate(merged)
        return cfg.settings

    new = await state.update_config(mutate, "settings")
    log.info("Ajustes modificados por «%s»: %s", p.username, ", ".join(sorted(body)))
    return json_response(new)


@router.get("/settings/retention")
async def get_retention(_: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    return json_response(state.config().settings.retention)


RGPD_MAX_RETENTION_DAYS = 30
RETENTION_WARNING = ("La retención supera 30 días. El art. 22.3 de la LOPDGDD obliga a borrar las imágenes en "
                     "un mes como máximo, salvo para acreditar un incidente concreto. Documenta el motivo en la EIPD.")


@router.put("/settings/retention")
async def put_retention(body: RetentionSettings, p: Principal = Depends(require_admin),
                        state: AppState = Depends(get_state)) -> Response:
    def mutate(cfg: AppConfig) -> RetentionSettings:
        cfg.settings.retention = body
        return body

    new = await state.update_config(mutate, "settings")
    log.info("Retención cambiada por «%s»: %d días, umbral de disco %d %%", p.username, new.days,
             new.disk_guard_percent)
    if new.days > RGPD_MAX_RETENTION_DAYS:
        log.warning("Retención de %d días fijada por «%s»: supera el mes que permite el art. 22.3 LOPDGDD "
                    "salvo que haya que conservar imágenes por un incidente concreto", new.days, p.username)
        return json_response({**new.model_dump(mode="json"), "warning": RETENTION_WARNING})
    return json_response(new)
