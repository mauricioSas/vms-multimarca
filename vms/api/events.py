"""Bus de eventos en memoria para Server-Sent Events (CONTRATO §6.11)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("vms.api.events")

ConfigScope = str  # "devices" | "cameras" | "walls" | "settings" | "analytics" | "users"


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


def format_sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")
