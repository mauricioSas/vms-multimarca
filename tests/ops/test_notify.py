"""Avisos por correo y webhook (CONTRATO §18.10, criterio 6 de B6).

Correo con un servidor SMTP simulado; webhook con un servidor HTTP real que COMPRUEBA la firma HMAC;
agrupación («5 cámaras sin vídeo en Tienda 37»), horas de silencio y secretos que nunca salen por la API.
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.ops.conftest import Harness
from tests.ops.doubles import SmtpDouble
from tools.mocks.server import MockHttpServer
from vms.core.models import NotificationRule, NotificationSettings, Site
from vms.ops.notify import SIGNATURE_HEADER, WEBHOOK_FAILED, Alert, Notifier, in_quiet_hours, sign, verify_signature


class WebhookReceiver:
    """Receptor de webhooks (como el de una CRA o n8n) que rechaza lo que no venga firmado con su secreto."""

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.accepted: list[dict[str, Any]] = []
        self.rejected = 0

        async def hook(request: Request) -> JSONResponse:
            body = await request.body()
            if not verify_signature(body, request.headers.get(SIGNATURE_HEADER, ""), self.secret):
                self.rejected += 1
                return JSONResponse({"error": "firma"}, 401)
            self.accepted.append(json.loads(body))
            return JSONResponse({"ok": True})

        self.server = MockHttpServer(Starlette(routes=[Route("/hook", hook, methods=["POST"])])).start()

    @property
    def url(self) -> str:
        return f"{self.server.base_url}/hook"


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Receptor en 127.0.0.1 por HTTP: en producción `netguard` lo rechaza (SSRF), así que aquí se permite."""
    from urllib.parse import urlsplit

    from vms.ops import netguard

    async def resolve_public(url: str) -> None:
        return None

    monkeypatch.setattr(netguard, "check_url", lambda url: urlsplit(url).hostname or "")
    monkeypatch.setattr(netguard, "resolve_public", resolve_public)
    r = WebhookReceiver("secreto-del-receptor-0123456789")
    yield r
    r.server.stop()


async def _configure(admin: Any, receiver: WebhookReceiver, smtp: SmtpDouble, **rule: Any) -> None:
    body = {"email_enabled": True, "smtp_host": "127.0.0.1", "smtp_port": smtp.port, "smtp_starttls": False,
            "smtp_username": "vms@tienda.es", "email_from": "vms@tienda.es", "email_to": ["mantenimiento@covert.es"],
            "webhook_enabled": True, "webhook_url": receiver.url,
            "rules": [{"kinds": ["camera_down", "tamper"], "min_severity": "warning", "channels": ["email", "webhook"],
                       "group_seconds": 0, **rule}]}
    r = await admin.put("/api/notifications/settings", json=body)
    assert r.status_code == 200, r.text
    r = await admin.put("/api/notifications/secrets", json={"smtp_password": "Clave-SMTP-1",
                                                            "webhook_secret": receiver.secret})
    assert r.status_code == 200, r.text


async def test_email_and_signed_webhook_end_to_end(api: Harness, receiver: WebhookReceiver) -> None:
    smtp = await SmtpDouble().start()
    admin = await api.login()
    await _configure(admin, receiver, smtp)
    settings = (await admin.get("/api/notifications/settings")).json()
    assert settings["has_smtp_password"] and settings["has_webhook_secret"]
    assert "Clave-SMTP-1" not in json.dumps(settings) and receiver.secret not in json.dumps(settings)

    ops = api.app.state.ops
    await ops.notifier.emit(Alert(kind="tamper", severity="critical", title_es="Cámara tapada: Cajas 2",
                                  camera_id="cam-1a2b3c4d", camera_name="Cajas 2",
                                  details={"score": 0, "causes": ["covered"]}))
    await asyncio.to_thread(lambda: None)
    assert len(receiver.accepted) == 1 and receiver.rejected == 0
    p = receiver.accepted[0]
    assert p["schema"] == 1 and p["kind"] == "tamper" and p["severity"] == "critical" and p["count"] == 1
    assert p["cameras"] == [{"camera_id": "cam-1a2b3c4d", "name": "Cajas 2"}]
    assert p["details"] == {"score": 0, "causes": ["covered"]} and p["at"].endswith("Z")
    assert set(p["site"]) == {"id", "name", "code"}
    assert len(smtp.messages) == 1 and smtp.logins == [("vms@tienda.es", "Clave-SMTP-1")]
    mail = email.message_from_string(smtp.messages[0]["data"], policy=email.policy.default)
    assert "Cámara tapada: Cajas 2" in str(mail["Subject"]) and smtp.messages[0]["rcpt"] == ["mantenimiento@covert.es"]
    assert "Cámara tapada: Cajas 2" in mail.get_body().get_content()   # type: ignore[union-attr]
    log = (await admin.get("/api/notifications/log")).json()
    assert {(x["channel"], x["ok"]) for x in log} == {("email", True), ("webhook", True)}

    # con otro secreto, el receptor lo rechaza y queda en el registro como fallido
    await admin.put("/api/notifications/secrets", json={"webhook_secret": "otro-secreto-distinto-0000"})
    rec = (await admin.post("/api/notifications/test", json={"channel": "webhook"})).json()
    # el error no dice el código HTTP (ni si el puerto estaba cerrado): no sirve para barrer puertos (SSRF)
    assert rec["ok"] is False and rec["error"] == WEBHOOK_FAILED and receiver.rejected == 1
    # «Generar secreto» lo enseña una sola vez
    r = (await admin.put("/api/notifications/secrets", json={"generate_webhook_secret": True})).json()
    assert len(r["webhook_secret"]) >= 32 and "webhook_secret" not in (await admin.get("/api/notifications/settings")).json()
    await smtp.stop()


async def test_grouping_five_cameras_one_message(api: Harness, receiver: WebhookReceiver) -> None:
    smtp = await SmtpDouble().start()
    admin = await api.login()
    await _configure(admin, receiver, smtp, group_seconds=60)
    ops = api.app.state.ops
    for i in range(5):
        await ops.notifier.emit(Alert(kind="camera_down", severity="critical", title_es=f"Cámara sin vídeo: Caja {i}",
                                      camera_id=f"cam-0000000{i}", camera_name=f"Caja {i}"))
    assert receiver.accepted == [] and smtp.messages == [], "dentro de la ventana no sale nada"
    await ops.notifier.flush_all()
    assert len(receiver.accepted) == 1 and len(smtp.messages) == 1
    p = receiver.accepted[0]
    assert p["count"] == 5 and p["title_es"].startswith("5 cámaras sin vídeo en ") and len(p["cameras"]) == 5
    await smtp.stop()


async def test_rules_severity_and_disabled_channels(api: Harness, receiver: WebhookReceiver) -> None:
    smtp = await SmtpDouble().start()
    admin = await api.login()
    await _configure(admin, receiver, smtp, min_severity="critical")
    ops = api.app.state.ops
    await ops.notifier.emit(Alert(kind="tamper", severity="warning", title_es="Imagen desenfocada: Puerta"))
    await ops.notifier.emit(Alert(kind="disk", severity="critical", title_es="Disco lleno"))   # tipo no incluido
    assert receiver.accepted == [] and smtp.messages == []
    r = await admin.put("/api/notifications/settings", json={"email_enabled": True, "smtp_host": "", "email_to": []})
    assert r.status_code == 422, "no se puede activar el correo sin servidor ni destinatarios"
    await smtp.stop()


def test_quiet_hours_cross_midnight() -> None:
    rule = NotificationRule(quiet_hours=("22:00", "07:00"))
    tz = "Europe/Madrid"
    assert in_quiet_hours(rule, datetime(2026, 10, 5, 21, 30, tzinfo=timezone.utc), tz)      # 23:30 en Madrid
    assert in_quiet_hours(rule, datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc), tz)       # 05:00
    assert not in_quiet_hours(rule, datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc), tz)  # 12:00


async def test_quiet_hours_let_critical_through_only(tmp_path: Any) -> None:
    from vms.core.credentials import CredentialStore, MemoryKeyring  # noqa: F401
    from vms.ops.store import OpsStore

    sent: list[str] = []

    async def webhook(url: str, body: bytes, headers: dict[str, str]) -> None:
        sent.append(json.loads(body)["severity"])

    class _Backend:
        name = "memoria"
        data = {"notify:webhook_secret": "x" * 20}

        def get(self, k: str) -> str | None:
            return self.data.get(k)

    creds = CredentialStore(_Backend())  # type: ignore[arg-type]
    cfg = NotificationSettings(webhook_enabled=True, webhook_url="http://127.0.0.1:1/hook",
                               rules=[NotificationRule(kinds=["tamper"], min_severity="warning", channels=["webhook"],
                                                       quiet_hours=("00:00", "23:59"), group_seconds=0)])
    n = Notifier(OpsStore(tmp_path / "o.sqlite3"), creds, lambda: cfg, lambda: Site(), webhook_sender=webhook)
    await n.emit(Alert(kind="tamper", severity="warning", title_es="a"))
    await n.emit(Alert(kind="tamper", severity="critical", title_es="b"))
    assert sent == ["critical"]


def test_signature_format() -> None:
    assert sign(b'{"a":1}', "k").startswith("sha256=") and len(sign(b"x", "k")) == 7 + 64
    assert verify_signature(b"x", sign(b"x", "k"), "k") and not verify_signature(b"y", sign(b"x", "k"), "k")
