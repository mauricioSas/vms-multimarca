"""Avisos por correo y webhook (CONTRATO §18.10). Dueño: B6.

| Método y ruta | Rol |
|---|---|
| `GET /api/notifications/settings` (nunca devuelve secretos: `has_smtp_password`, `has_webhook_secret`) | A |
| `PUT /api/notifications/settings` | A |
| `PUT /api/notifications/secrets` `{"smtp_password"?, "webhook_secret"?, "generate_webhook_secret"?}` | A |
| `POST /api/notifications/test` `{"channel": "email"\\|"webhook"\\|"telegram"}` | A |
| `GET /api/notifications/log` | A |
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import time
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field, SecretStr
from starlette.responses import Response

from vms.core.audit import audit
from vms.core.errors import ValidationFailed
from vms.core.models import AppConfig, NotificationSettings, known_fields_only
from vms.ops import netguard
from vms.ops.notify import SMTP_PASSWORD_KEY, WEBHOOK_SECRET_KEY

from ..deps import Principal, get_state, require_admin
from ..errors import json_response
from ..security import client_ip
from ..state import AppState
from .health import get_ops

if TYPE_CHECKING:
    from vms.ops.service import OpsService

log = logging.getLogger("vms.api.notifications")
router = APIRouter(prefix="/api/notifications", tags=["notifications"])

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _public(state: AppState) -> dict[str, Any]:
    cfg = state.config().settings.notifications
    backend = state.creds.backend
    return {**cfg.model_dump(mode="json"), "has_smtp_password": bool(backend.get(SMTP_PASSWORD_KEY)),
            "has_webhook_secret": bool(backend.get(WEBHOOK_SECRET_KEY))}


def _validate(cfg: NotificationSettings) -> None:
    errors: list[dict[str, Any]] = []
    for i, addr in enumerate(cfg.email_to):
        if not re.match(EMAIL_RE, addr):
            errors.append({"loc": ["email_to", i], "msg": "Correo no válido"})
    if cfg.email_from and not re.match(EMAIL_RE, cfg.email_from):
        errors.append({"loc": ["email_from"], "msg": "Correo no válido"})
    if cfg.email_enabled and (not cfg.smtp_host or not cfg.email_to or not cfg.email_from):
        errors.append({"loc": ["smtp_host"], "msg": "Para activar el correo faltan el servidor, el remitente o los "
                                                     "destinatarios"})
    if cfg.webhook_url:
        try:
            netguard.check_url(cfg.webhook_url)   # https y servidor público (al enviar se comprueba también el DNS)
        except netguard.UnsafeUrl as exc:
            errors.append({"loc": ["webhook_url"], "msg": str(exc)})
    if cfg.webhook_enabled and not cfg.webhook_url:
        errors.append({"loc": ["webhook_url"], "msg": "Falta la dirección del webhook"})
    for i, rule in enumerate(cfg.rules):
        if rule.quiet_hours:
            try:
                time.fromisoformat(rule.quiet_hours[0])
                time.fromisoformat(rule.quiet_hours[1])
            except ValueError:
                errors.append({"loc": ["rules", i, "quiet_hours"], "msg": "Usa horas como 22:00"})
    if errors:
        raise ValidationFailed("Hay datos no válidos en los avisos", details={"fields": errors})


@router.get("/settings")
async def get_settings(_: Principal = Depends(require_admin), state: AppState = Depends(get_state)) -> Response:
    return json_response(_public(state))


@router.put("/settings")
async def put_settings(request: Request, body: dict[str, Any] = Body(...), p: Principal = Depends(require_admin),
                       state: AppState = Depends(get_state)) -> Response:
    clean = {k: v for k, v in body.items() if k not in ("has_smtp_password", "has_webhook_secret")}
    new = NotificationSettings.model_validate(known_fields_only(NotificationSettings, clean))
    _validate(new)

    def mutate(cfg: AppConfig) -> None:
        cfg.settings.notifications = new

    await state.update_config(mutate, "settings")
    audit("notifications_settings", user=p.username, ip=client_ip(request), email=new.email_enabled,
          webhook=new.webhook_enabled, rules=len(new.rules))
    return json_response(_public(state))


class SecretsBody(BaseModel):
    smtp_password: SecretStr | None = None
    webhook_secret: SecretStr | None = Field(None, description="Vacío = borrar")
    generate_webhook_secret: bool = False


@router.put("/secrets")
async def put_secrets(body: SecretsBody, request: Request, p: Principal = Depends(require_admin),
                      state: AppState = Depends(get_state)) -> Response:
    backend = state.creds.backend
    out: dict[str, Any] = {}
    if body.smtp_password is not None:
        v = body.smtp_password.get_secret_value()
        await asyncio.to_thread(backend.set if v else (lambda k, _v: backend.delete(k)), SMTP_PASSWORD_KEY, v)
    if body.generate_webhook_secret:
        generated = secrets.token_urlsafe(32)
        await asyncio.to_thread(backend.set, WEBHOOK_SECRET_KEY, generated)
        out["webhook_secret"] = generated        # se enseña UNA vez para configurarlo en el receptor
    elif body.webhook_secret is not None:
        v = body.webhook_secret.get_secret_value()
        if v and len(v) < 16:
            raise ValidationFailed("El secreto del webhook necesita al menos 16 caracteres",
                                   details={"fields": [{"loc": ["webhook_secret"], "msg": "Mínimo 16 caracteres"}]})
        await asyncio.to_thread(backend.set if v else (lambda k, _v: backend.delete(k)), WEBHOOK_SECRET_KEY, v)
    audit("notifications_secrets", user=p.username, ip=client_ip(request),
          smtp_password=body.smtp_password is not None,
          webhook_secret=body.webhook_secret is not None or body.generate_webhook_secret)
    return json_response({**_public(state), **out})


class TestBody(BaseModel):
    channel: Literal["email", "webhook", "telegram"]


@router.post("/test")
async def test_channel(body: TestBody, _: Principal = Depends(require_admin),
                       ops: "OpsService" = Depends(get_ops)) -> Response:
    rec = await ops.notifier.test(body.channel)
    return json_response(rec)


@router.get("/log")
async def get_log(_: Principal = Depends(require_admin), ops: "OpsService" = Depends(get_ops)) -> Response:
    return json_response(await asyncio.to_thread(ops.store.notifications))
