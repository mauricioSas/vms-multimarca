"""Auditoría de seguridad de los equipos (CONTRATO §18.12). Dueño: B6.

`POST /api/security-audit/run` (A) `{"admin_credentials": {"<device_id>": {"username", "password"}}}` (opcional;
las credenciales de administrador se usan una vez y **no se guardan ni se registran**) → `SecurityAuditReport`;
`GET /api/security-audit/latest` (A); `GET /api/security-audit/advisories` (A) → qué tabla de avisos se usa.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field, SecretStr
from starlette.responses import Response

from vms.core.errors import NotFoundError
from vms.ops.security.advisories import load_table

from ..deps import Principal, get_state, require_admin
from ..errors import json_response
from ..security import client_ip
from ..state import AppState
from .health import get_ops

if TYPE_CHECKING:
    from vms.ops.service import OpsService

log = logging.getLogger("vms.api.security_audit")
router = APIRouter(prefix="/api/security-audit", tags=["security_audit"])


class AdminCredential(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr

    def __repr__(self) -> str:   # nunca en trazas
        return f"AdminCredential(username={self.username!r})"


class RunBody(BaseModel):
    admin_credentials: dict[str, AdminCredential] = Field(default_factory=dict, max_length=256)


@router.post("/run")
async def run(request: Request, body: RunBody = Body(default=RunBody()), p: Principal = Depends(require_admin),
              state: AppState = Depends(get_state), ops: "OpsService" = Depends(get_ops)) -> Response:
    cfg = state.config()
    creds = {did: (c.username, c.password.get_secret_value()) for did, c in body.admin_credentials.items()
             if cfg.device(did) is not None and c.password.get_secret_value()}
    report = await ops.security_audit(creds, p.username, client_ip(request))
    creds.clear()
    return json_response(report)


@router.get("/latest")
async def latest(_: Principal = Depends(require_admin), ops: "OpsService" = Depends(get_ops)) -> Response:
    report = ops.latest_audit()
    if report is None:
        raise NotFoundError("Todavía no se ha hecho ninguna auditoría")
    return json_response(report)


@router.get("/advisories")
async def advisories(_: Principal = Depends(require_admin), state: AppState = Depends(get_state)) -> Response:
    loaded = load_table(state.paths.base)
    t = loaded.table
    return json_response({"origin": loaded.origin, "generated_at": t.generated_at, "kev_catalog_version":
                          t.kev_catalog_version, "source": t.source, "advisories": len(t.advisories),
                          "nvd_notice": t.nvd_notice})
