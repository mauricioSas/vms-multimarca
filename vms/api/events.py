"""Bus de eventos en memoria para Server-Sent Events (CONTRATO §6.11 y §17.3). Dueño: B2.

Eventos de la v1: `config` y `status`. Eventos nuevos de la v2, declarados en la fase 0 con su
carga (TypedDict) para que nadie invente nombres: los emiten sus dueños con las funciones de abajo.

| Evento     | Lo emite | Carga |
|------------|----------|-------|
| `engine`   | B1       | EngineEvent: el motor arrancó, se reinició o cayó (los muros ponen a cero su espera) |
| `update`   | B4       | UpdateEvent: versión activa nueva o estado de actualización |
| `health`   | B6       | HealthEvent: cambio de salud de imagen o de hora de una cámara |
| `bookmark` | B6       | BookmarkEvent: marcador creado, cambiado o borrado (repinta la línea de tiempo) |
| `evidence` | B6       | EvidenceEvent: progreso de una exportación de evidencias |
| `notice`   | B6       | NoticeEvent: aviso agrupado para la interfaz (nunca en los muros en kiosco) |
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, TypedDict

log = logging.getLogger("vms.api.events")

ConfigScope = str  # "devices" | "cameras" | "walls" | "settings" | "analytics" | "users"

# ----------------------------------------------------------------------------- nombres (v2)
EVENT_ENGINE = "engine"
EVENT_UPDATE = "update"
EVENT_HEALTH = "health"
EVENT_BOOKMARK = "bookmark"
EVENT_EVIDENCE = "evidence"
EVENT_NOTICE = "notice"
V2_EVENTS = (EVENT_ENGINE, EVENT_UPDATE, EVENT_HEALTH, EVENT_BOOKMARK, EVENT_EVIDENCE, EVENT_NOTICE)


class EngineEvent(TypedDict):
    state: Literal["started", "restarted", "down"]
    pid: int | None
    at: str                                   # ISO 8601 UTC


class UpdateEvent(TypedDict, total=False):
    version: str                              # versión activa
    viewer_restart: bool                      # el visor debe reiniciarse (cambió el componente viewer)
    state: str                                # estado del diario (CONTRATO §15.3)
    message_es: str


class HealthEvent(TypedDict):
    camera_id: str
    score: int | None                         # 0-100; None = sin referencia o sin comprobar
    status: Literal["ok", "warning", "critical", "unknown"]
    causes: list[str]                         # códigos estables (CONTRATO §18.2)
    clock_skew_s: float | None


class BookmarkEvent(TypedDict):
    action: Literal["created", "updated", "deleted"]
    bookmark_id: str
    camera_id: str


class EvidenceEvent(TypedDict, total=False):
    export_id: str
    state: Literal["queued", "running", "done", "failed"]
    progress: float                           # 0..1
    message_es: str


class NoticeEvent(TypedDict):
    severity: Literal["info", "warning", "critical"]
    kind: str
    title_es: str
    count: int                                # agrupación: «5 cámaras caídas»


class EventBus:
    def __init__(self, queue_size: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[tuple[str, str]]] = set()
        self.queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[tuple[str, str]]:
        q: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[str, str]]) -> None:
        self._subscribers.discard(q)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        for q in list(self._subscribers):
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                # Cliente que no lee (pestaña colgada): se descarta lo más viejo para no crecer sin límite
                try:
                    q.get_nowait()
                    q.put_nowait((event, payload))
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.debug("Cola SSE saturada; evento descartado")


def publish_engine(bus: EventBus, ev: EngineEvent) -> None:
    bus.publish(EVENT_ENGINE, dict(ev))


def publish_update(bus: EventBus, ev: UpdateEvent) -> None:
    bus.publish(EVENT_UPDATE, dict(ev))


def publish_health(bus: EventBus, ev: HealthEvent) -> None:
    bus.publish(EVENT_HEALTH, dict(ev))


def publish_bookmark(bus: EventBus, ev: BookmarkEvent) -> None:
    bus.publish(EVENT_BOOKMARK, dict(ev))


def publish_evidence(bus: EventBus, ev: EvidenceEvent) -> None:
    bus.publish(EVENT_EVIDENCE, dict(ev))


def publish_notice(bus: EventBus, ev: NoticeEvent) -> None:
    bus.publish(EVENT_NOTICE, dict(ev))


def format_sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")
