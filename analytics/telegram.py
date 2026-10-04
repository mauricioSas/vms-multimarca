"""Avisos por Telegram (Bot API `sendMessage`), solo texto: nunca se envían imágenes.

Configuración:
- El token del bot va en el entorno del proceso (`VMS_TELEGRAM_BOT_TOKEN`), nunca en config.json.
- El chat (grupo) y si está activado vienen de la configuración de la sede (panel → Ajustes).

Si Telegram falla (sin Internet, límite de mensajes...), se reintenta con esperas crecientes. Un
fallo de Telegram nunca frena el conteo: los envíos van en tareas aparte y el resultado se anota
en `queue_alerts.notified_at` / `notify_error`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from vms.core.rtsp import redact

from .alerts import AlertEvent

log = logging.getLogger("analytics.telegram")

API_BASE = "https://api.telegram.org"
RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)


class TelegramError(Exception):
    """Fallo definitivo al enviar (tras los reintentos). Mensaje en español y sin el token."""


class TelegramNotifier:
    def __init__(self, token: str, *, api_base: str = API_BASE, timeout: float = 10.0,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not token:
            raise ValueError("Falta el token del bot de Telegram")
        self._url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
        self._delays = retry_delays
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, chat_id: str, text: str) -> None:
        """Envía un mensaje. Reintenta errores temporales; lanza TelegramError si no lo logra."""
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        attempts = len(self._delays) + 1
        last = ""
        for attempt in range(attempts):
            try:
                resp = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                last = redact(f"sin conexión con Telegram ({type(exc).__name__}: {exc})")
                retry_after = None
            else:
                if resp.status_code == 200:
                    return
                desc = _description(resp)
                if resp.status_code == 429:
                    last = f"Telegram limita los envíos: {desc}"
                    retry_after = _retry_after(resp)
                elif resp.status_code >= 500:
                    last = f"Telegram respondió {resp.status_code}: {desc}"
                    retry_after = None
                else:
                    # 400 (chat inexistente), 401 (token malo), 403 (bot expulsado): no se arregla reintentando.
                    raise TelegramError(_explain(resp.status_code, desc))
            if attempt == attempts - 1:
                break
            delay = self._delays[min(attempt, len(self._delays) - 1)]
            if retry_after is not None:
                delay = max(delay, float(retry_after))
            log.warning("Envío a Telegram fallido (%s); reintento %d en %.0f s", last, attempt + 1, delay)
            await asyncio.sleep(delay)
        raise TelegramError(last or "error desconocido al enviar a Telegram")


def _description(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("description", ""))[:200]
    except ValueError:
        return resp.text[:200]


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        value = resp.json().get("parameters", {}).get("retry_after")
        return float(value) if value is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


def _explain(status: int, desc: str) -> str:
    if status == 401:
        return "Telegram rechaza el token del bot (revisa VMS_TELEGRAM_BOT_TOKEN)"
    if status == 400 and "chat not found" in desc.lower():
        return "Telegram no encuentra el chat (revisa el ID del grupo y que el bot esté dentro)"
    if status == 403:
        return "El bot no puede escribir en ese chat (¿lo expulsaron del grupo?)"
    return f"Telegram rechazó el mensaje ({status}): {desc}"


# =========================================================================== textos
def _local(dt: datetime, tz: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return dt.astimezone(timezone.utc)


def _minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return "1 minuto" if m == 1 else f"{m} minutos"


def alert_started_text(ev: AlertEvent, site_name: str, camera_name: str, tz: str) -> str:
    hhmm = _local(ev.started_at, tz).strftime("%H:%M")
    return (f"COLA EN CAJAS · {site_name}\n"
            f"Zona «{ev.rule_name}» ({camera_name}): {ev.occupancy} personas en cola desde las {hhmm} "
            f"(aviso a partir de {ev.threshold}).\n"
            f"Conviene abrir otra caja.")


def alert_ended_text(ev: AlertEvent, site_name: str, camera_name: str, tz: str) -> str:
    end = ev.ended_at or datetime.now(timezone.utc)
    hhmm = _local(end, tz).strftime("%H:%M")
    return (f"Cola normalizada · {site_name}\n"
            f"Zona «{ev.rule_name}» ({camera_name}) a las {hhmm}: {ev.occupancy} personas. "
            f"La cola duró {_minutes(ev.duration_seconds)} (máximo {ev.peak_people} personas).")
