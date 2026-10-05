"""Avisos por correo, webhook y Telegram con reglas y agrupación (CONTRATO §18.10).

- Reglas (`NotificationRule` en `settings.notifications.rules`): tipos, gravedad mínima, canales, horas de
  silencio (hora local de la sede) y ventana de **agrupación**: los avisos parecidos que llegan dentro
  de `group_seconds` salen en un solo mensaje («5 cámaras caídas en Tienda 37»).
- Correo con `smtplib` (biblioteca estándar) en un hilo; webhook con `httpx` y firma
  `X-VMS-Signature: sha256=<HMAC-SHA256 hex del cuerpo>`; Telegram como en la v1.
- Secretos en el `CredentialStore`: `notify:smtp_password` y `notify:webhook_secret`. Nunca en
  config.json, nunca en registros ni en respuestas de la API.
- Por defecto, **sin imágenes** (RGPD). Cada envío queda en `notifications_log` (90 días) y la interfaz
  recibe el evento SSE `notice` (nunca en los muros).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import smtplib
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from email.message import EmailMessage
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from vms.core.credentials import CredentialStore
from vms.core.models import NotificationRule, NotificationSettings, Site
from vms.core.rtsp import redact

from . import netguard
from .models import NotificationRecord, Severity
from .store import OpsStore

log = logging.getLogger("vms.ops.notify")

SMTP_PASSWORD_KEY = "notify:smtp_password"
WEBHOOK_SECRET_KEY = "notify:webhook_secret"
SIGNATURE_HEADER = "X-VMS-Signature"
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
Channel = Literal["email", "webhook", "telegram"]

# Título agrupado por tipo: (singular con {name}, plural con {n})
GROUP_TITLES: dict[str, tuple[str, str]] = {
    "camera_down": ("Cámara sin vídeo: {name}", "{n} cámaras sin vídeo"),
    "camera_up": ("Cámara recuperada: {name}", "{n} cámaras recuperadas"),
    "tamper": ("Problema de imagen: {name}", "{n} cámaras con problemas de imagen"),
    "tamper_cleared": ("Imagen recuperada: {name}", "{n} cámaras con la imagen recuperada"),
    "clock_skew": ("Hora desajustada: {name}", "{n} equipos con la hora desajustada"),
    "disk": ("Disco de grabación: {name}", "{n} avisos de disco"),
    "retention_forecast": ("Previsión de grabación: {name}", "{n} avisos de previsión de grabación"),
    "recording_gap": ("Hueco de grabación: {name}", "{n} huecos de grabación"),
    "recording_gap_cleared": ("Vuelve a grabar: {name}", "{n} cámaras vuelven a grabar"),
    "test": ("Aviso de prueba", "{n} avisos de prueba"),
}


@dataclass
class Alert:
    kind: str
    severity: Severity
    title_es: str
    camera_id: str | None = None
    camera_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class _Group:
    rule_index: int
    kind: str
    severity: Severity
    alerts: list[Alert]
    task: asyncio.Task[None] | None = None


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str, secret: str) -> bool:
    return hmac.compare_digest(sign(body, secret), header or "")


def in_quiet_hours(rule: NotificationRule, now: datetime, tz_name: str) -> bool:
    if not rule.quiet_hours:
        return False
    try:
        tz: Any = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    try:
        a = time.fromisoformat(rule.quiet_hours[0])
        b = time.fromisoformat(rule.quiet_hours[1])
    except ValueError:
        return False
    t = now.astimezone(tz).time()
    return (a <= t < b) if a <= b else (t >= a or t < b)


# Recuperación → aviso que cierra. Las recuperaciones son «info»: con la gravedad mínima por defecto
# («warning») no saldrían nunca; por eso una recuperación sale SIEMPRE que su aviso salió por esa regla.
RECOVERY_OF = {"camera_up": "camera_down", "tamper_cleared": "tamper", "recording_gap_cleared": "recording_gap"}


def rule_matches(rule: NotificationRule, kind: str, severity: str) -> bool:
    base = kind.removesuffix("_cleared").removesuffix("_up")
    kinds = set(rule.kinds)
    ok_kind = kind in kinds or base in kinds or (kind == "camera_up" and "camera_down" in kinds)
    return ok_kind and SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(rule.min_severity, 1)


def group_title(kind: str, alerts: list[Alert], site: Site) -> str:
    if len(alerts) == 1:
        return alerts[0].title_es
    _, plural = GROUP_TITLES.get(kind, ("{name}", "{n} avisos"))
    return f"{plural.format(n=len(alerts))} en {site.name}"


EmailSender = Callable[[NotificationSettings, str, str, str], Awaitable[None]]
WebhookSender = Callable[[str, bytes, dict[str, str]], Awaitable[None]]
TelegramSender = Callable[[str, str, str], Awaitable[None]]
NoticePublisher = Callable[[dict[str, Any]], None]


async def smtp_send(cfg: NotificationSettings, password: str, subject: str, body: str) -> None:
    """Envía un correo (en un hilo: smtplib es bloqueante). Lanza OSError/smtplib.SMTPException."""
    if not cfg.smtp_host or not cfg.email_to or not cfg.email_from:
        raise ValueError("Faltan el servidor, el remitente o los destinatarios del correo")

    def send() -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.email_from
        msg["To"] = ", ".join(cfg.email_to)
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if cfg.smtp_port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15, context=ctx)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
        with server:
            if cfg.smtp_port != 465 and cfg.smtp_starttls:
                server.starttls(context=ctx)
            if cfg.smtp_username:
                server.login(cfg.smtp_username, password)
            server.send_message(msg)

    await asyncio.to_thread(send)


WEBHOOK_FAILED = "el servidor del webhook no aceptó el aviso (sin conexión o respondió con un error)"


async def http_post(url: str, body: bytes, headers: dict[str, str]) -> None:
    """POST del webhook. Solo a servidores públicos por HTTPS (`netguard`) y sin seguir redirecciones. El error
    que se guarda (y ve el administrador) es el mismo si no conecta o si responde con error: no sirve para barrer
    puertos; el detalle va solo al registro de depuración."""
    await netguard.resolve_public(url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False) as client:
            r = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        log.debug("Webhook: %s", type(exc).__name__)
        raise RuntimeError(WEBHOOK_FAILED) from None
    if r.status_code >= 300:
        log.debug("Webhook: HTTP %s", r.status_code)
        raise RuntimeError(WEBHOOK_FAILED)


async def telegram_send(token: str, chat_id: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": text})
    if r.status_code >= 300:
        raise RuntimeError(f"Telegram respondió {r.status_code}")


class Notifier:
    def __init__(self, store: OpsStore, creds: CredentialStore, settings: Callable[[], NotificationSettings],
                 site: Callable[[], Site], *, telegram: Callable[[], tuple[str, str] | None] = lambda: None,
                 publish_notice: NoticePublisher | None = None, email_sender: EmailSender | None = None,
                 webhook_sender: WebhookSender | None = None, telegram_sender: TelegramSender | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.creds = creds
        self.settings = settings
        self.site = site
        self.telegram = telegram
        self.publish_notice = publish_notice
        self.email_sender: EmailSender = email_sender or (lambda c, p, s, b: smtp_send(c, p, s, b))
        self.webhook_sender: WebhookSender = webhook_sender or http_post
        self.telegram_sender: TelegramSender = telegram_sender or telegram_send
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._groups: dict[tuple[int, str, str], _Group] = {}
        self._pending: set[asyncio.Task[None]] = set()
        self._open: set[tuple[int, str, str]] = set()     # (regla, tipo, cámara) de avisos enviados sin cerrar

    # ------------------------------------------------------------------ entrada
    async def emit(self, alert: Alert) -> None:
        """Un aviso nuevo: se agrupa por regla, tipo y gravedad durante `group_seconds`."""
        cfg = self.settings()
        site = self.site()
        if self.publish_notice is not None:
            self.publish_notice({"severity": alert.severity, "kind": alert.kind, "title_es": alert.title_es,
                                 "count": 1, "camera_ids": [alert.camera_id] if alert.camera_id else []})
        problem = RECOVERY_OF.get(alert.kind)
        for i, rule in enumerate(cfg.rules):
            if not rule.channels:
                continue
            recovers_sent = False
            if problem is not None:
                okey = (i, problem, alert.camera_id or "")
                recovers_sent = okey in self._open
                self._open.discard(okey)
            if not recovers_sent and not rule_matches(rule, alert.kind, alert.severity):
                continue
            if (not recovers_sent and in_quiet_hours(rule, alert.at, site.timezone)
                    and alert.severity != "critical"):
                continue
            if alert.kind in RECOVERY_OF.values():
                self._open.add((i, alert.kind, alert.camera_id or ""))
            key = (i, alert.kind, alert.severity)
            group = self._groups.get(key)
            if group is not None:
                group.alerts.append(alert)
                continue
            group = _Group(i, alert.kind, alert.severity, [alert])
            self._groups[key] = group
            if rule.group_seconds <= 0:
                await self._flush(key)
            else:
                group.task = asyncio.get_running_loop().create_task(self._flush_later(key, rule.group_seconds),
                                                                    name=f"notify-group-{alert.kind}")
                self._pending.add(group.task)
                group.task.add_done_callback(self._pending.discard)

    async def _flush_later(self, key: tuple[int, str, str], delay: float) -> None:
        await asyncio.sleep(delay)
        await self._flush(key)

    async def flush_all(self) -> None:
        """Envía ya todo lo agrupado (al parar y en pruebas)."""
        for key in list(self._groups):
            g = self._groups.get(key)
            if g is not None and g.task is not None and not g.task.done():
                g.task.cancel()
            await self._flush(key)

    async def _flush(self, key: tuple[int, str, str]) -> None:
        group = self._groups.pop(key, None)
        if group is None:
            return
        cfg = self.settings()
        if group.rule_index >= len(cfg.rules):
            return
        rule = cfg.rules[group.rule_index]
        site = self.site()
        title = group_title(group.kind, group.alerts, site)
        if len(group.alerts) > 1 and self.publish_notice is not None:
            self.publish_notice({"severity": group.severity, "kind": group.kind, "title_es": title,
                                 "count": len(group.alerts),
                                 "camera_ids": sorted({a.camera_id for a in group.alerts if a.camera_id})})
        for channel in rule.channels:
            await self._send(channel, cfg, site, group.kind, group.severity, title, group.alerts)

    # ------------------------------------------------------------------ salida
    def payload(self, site: Site, kind: str, severity: str, title: str, alerts: list[Alert]) -> dict[str, Any]:
        cams = [{"camera_id": a.camera_id, "name": a.camera_name or ""} for a in alerts if a.camera_id]
        details: dict[str, Any] = alerts[0].details if len(alerts) == 1 else {"items": [a.title_es for a in alerts[:20]]}
        return {"schema": 1, "site": {"id": site.id, "name": site.name, "code": site.code},
                "at": max(a.at for a in alerts).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "severity": severity, "kind": kind, "title_es": title, "count": len(alerts), "cameras": cams,
                "details": details}

    async def _send(self, channel: Channel, cfg: NotificationSettings, site: Site, kind: str, severity: Severity,
                    title: str, alerts: list[Alert]) -> NotificationRecord:
        ok, error = True, ""
        try:
            if channel == "email":
                if not cfg.email_enabled:
                    raise RuntimeError("el correo está desactivado")
                lines = [title, "", f"Sede: {site.name} ({site.code or site.id})", f"Gravedad: {_sev_es(severity)}", ""]
                lines += [f"- {a.title_es}" for a in alerts[:50]]
                lines += ["", "Aviso automático del VMS. Para cambiar qué avisos llegan, entra en Estado del sistema "
                          "> Avisos."]
                await self.email_sender(cfg, self.creds.backend.get(SMTP_PASSWORD_KEY) or "",
                                        f"[VMS {site.name}] {title}", "\n".join(lines))
            elif channel == "webhook":
                if not cfg.webhook_enabled or not cfg.webhook_url:
                    raise RuntimeError("el webhook está desactivado o sin URL")
                secret = self.creds.backend.get(WEBHOOK_SECRET_KEY)
                if not secret:
                    raise RuntimeError("falta el secreto del webhook (genéralo en los ajustes de avisos)")
                body = json.dumps(self.payload(site, kind, severity, title, alerts), ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")
                await self.webhook_sender(cfg.webhook_url, body, {
                    "Content-Type": "application/json", SIGNATURE_HEADER: sign(body, secret),
                    "X-VMS-Event": kind, "User-Agent": "VMS-Multimarca"})
            elif channel == "telegram":
                tg = self.telegram()
                if tg is None:
                    raise RuntimeError("Telegram no está configurado")
                await self.telegram_sender(tg[0], tg[1], f"{title}\n" + "\n".join(f"- {a.title_es}" for a in alerts[:20]))
        except Exception as exc:  # noqa: BLE001 - un canal roto no puede impedir el resto
            ok, error = False, redact(str(exc))[:300] or type(exc).__name__
            log.warning("No se pudo enviar el aviso «%s» por %s: %s", title, channel, error)
        rec = NotificationRecord(id=f"nt-{secrets.token_hex(6)}", at=self.clock(), kind=kind, severity=severity,
                                 channel=channel, grouped=len(alerts), title_es=title, ok=ok, error=error)
        await asyncio.to_thread(self.store.add_notification, rec)
        return rec

    async def test(self, channel: Channel) -> NotificationRecord:
        """«Probar»: un aviso de prueba por un canal, sin reglas ni agrupación."""
        cfg = self.settings()
        site = self.site()
        alert = Alert(kind="test", severity="info", title_es=f"Aviso de prueba de {site.name}",
                      details={"message_es": "Si te llega, el canal funciona."})
        return await self._send(channel, cfg, site, "test", "info", alert.title_es, [alert])

    async def close(self) -> None:
        for t in list(self._pending):
            t.cancel()
        self._groups.clear()


def _sev_es(severity: str) -> str:
    return {"info": "informativo", "warning": "aviso", "critical": "crítico"}.get(severity, severity)
