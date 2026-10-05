"""Estado del onboarding por usuario (CONTRATO §18.14). Dueño: B6.

`GET /api/onboarding/state` y `PUT /api/onboarding/state` (O) → `OnboardingState` (en `ops/onboarding.json`).
Cada usuario solo lee y cambia el suyo. El kiosco no tiene onboarding (nunca hay recorridos en los muros).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends
from starlette.responses import Response

from vms.ops.models import OnboardingState

from ..deps import Principal, get_state, require_operator
from ..errors import json_response
from ..state import AppState
from .health import get_ops

if TYPE_CHECKING:
    from vms.ops.service import OpsService

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def _payload(state: AppState, ops: "OpsService", p: Principal) -> dict[str, Any]:
    st = ops.onboarding.get(p.username)
    cfg = state.config()
    return {**st.model_dump(mode="json"), "role": p.role, "has_devices": bool(cfg.devices),
            "has_cameras": bool(cfg.cameras),
            "show_wizard": p.role == "admin" and not cfg.devices and not st.wizard_completed}


@router.get("/state")
async def get_state_(p: Principal = Depends(require_operator), state: AppState = Depends(get_state),
                     ops: "OpsService" = Depends(get_ops)) -> Response:
    return json_response(_payload(state, ops, p))


@router.put("/state")
async def put_state(body: dict[str, Any] = Body(...), p: Principal = Depends(require_operator),
                    state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    current = ops.onboarding.get(p.username)
    fields = {k: v for k, v in body.items() if k in OnboardingState.model_fields and k != "username"}
    new = OnboardingState.model_validate({**current.model_dump(), **fields, "username": p.username})
    ops.onboarding.put(new)
    return json_response(_payload(state, ops, p))
