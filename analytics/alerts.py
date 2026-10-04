"""Lógica de alerta de cola: «más de X personas durante más de Y segundos».

Máquina de estados por zona (sin red ni base de datos: fácil de probar):

    tranquila ──(ocupación ≥ umbral)──▶ por encima ──(lleva ≥ alert_min_seconds)──▶ ALERTA
        ▲                                   │                                         │
        └──────(baja del umbral)────────────┘                                         │
        └────────────(ocupación ≤ clear_below durante 30 s seguidos)──────────────────┘

- `alert_threshold` (X): personas en la zona para considerar que hay cola.
- `alert_min_seconds` (Y): tiempo SEGUIDO por encima del umbral antes de avisar. Evita avisos
  por un pico de un segundo (un grupo que pasa por delante).
- `alert_cooldown_seconds`: tiempo mínimo entre el inicio de dos avisos de la misma zona, para
  no saturar el grupo de Telegram. Si la cola sigue ahí cuando vence, se vuelve a avisar.
- `clear_below`: fin de la alerta cuando la ocupación baja a este valor o menos (por defecto
  umbral − 1) durante 30 s seguidos. Así no «parpadea» si oscila alrededor del umbral.

Los tiempos se miden con un reloj monótono (no le afectan los cambios de hora del PC); las fechas
que se guardan (inicio/fin) son UTC.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from vms.core.models import ZoneRule


@dataclass(frozen=True)
class AlertEvent:
    kind: Literal["started", "ended"]
    alert_id: str
    rule_id: str
    camera_id: str
    rule_name: str
    started_at: datetime
    ended_at: datetime | None
    peak_people: int
    threshold: int
    occupancy: int

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())


class QueueAlertMonitor:
    def __init__(self, rule: ZoneRule, *, clear_seconds: float = 30.0) -> None:
        self.rule = rule
        self.clear_seconds = clear_seconds
        self._over_since: float | None = None        # reloj monótono
        self._over_since_wall: datetime | None = None
        self._pre_peak = 0
        self._below_since: float | None = None
        self._last_alert_start: float | None = None
        self.active: AlertEvent | None = None
        self._peak = 0

    @property
    def threshold(self) -> int:
        return self.rule.alert_threshold

    @property
    def clear_below(self) -> int:
        default = self.rule.alert_threshold - 1
        # Un valor >= umbral terminaría la alerta con la cola todavía llena (la API ya no lo admite,
        # pero un config.json antiguo podría traerlo): nunca por encima de umbral - 1.
        return min(self.rule.clear_below, default) if self.rule.clear_below is not None else default

    def update(self, occupancy: int, now: float, wall: datetime) -> list[AlertEvent]:
        """Nueva muestra de ocupación. Devuelve los eventos producidos (0 o 1)."""
        events: list[AlertEvent] = []
        if self.active is not None:
            self._peak = max(self._peak, occupancy)
            if occupancy <= self.clear_below:
                if self._below_since is None:
                    self._below_since = now
                if now - self._below_since >= self.clear_seconds:
                    events.append(self._end(wall, occupancy))
            else:
                self._below_since = None
            return events

        if occupancy >= self.threshold:
            if self._over_since is None:
                self._over_since, self._over_since_wall, self._pre_peak = now, wall, occupancy
            self._pre_peak = max(self._pre_peak, occupancy)
            long_enough = now - self._over_since >= self.rule.alert_min_seconds
            cooled = (self._last_alert_start is None
                      or now - self._last_alert_start >= self.rule.alert_cooldown_seconds)
            if long_enough and cooled:
                self._last_alert_start = now
                self._peak = self._pre_peak
                self._below_since = None
                self.active = AlertEvent(
                    kind="started", alert_id=str(uuid.uuid4()), rule_id=self.rule.id, camera_id=self.rule.camera_id,
                    rule_name=self.rule.name, started_at=self._over_since_wall or wall, ended_at=None,
                    peak_people=self._peak, threshold=self.threshold, occupancy=occupancy)
                events.append(self.active)
        else:
            self._over_since = None
            self._over_since_wall = None
            self._pre_peak = 0
        return events

    def close(self, wall: datetime, occupancy: int = 0) -> AlertEvent | None:
        """Cierra una alerta abierta (al parar la cámara o el servicio)."""
        if self.active is None:
            return None
        return self._end(wall, occupancy)

    def _end(self, wall: datetime, occupancy: int) -> AlertEvent:
        assert self.active is not None
        started = self.active.started_at
        ended = AlertEvent(kind="ended", alert_id=self.active.alert_id, rule_id=self.active.rule_id,
                           camera_id=self.active.camera_id, rule_name=self.rule.name, started_at=started,
                           ended_at=max(wall, started), peak_people=self._peak, threshold=self.active.threshold,
                           occupancy=occupancy)
        self.active = None
        self._over_since = None
        self._over_since_wall = None
        self._below_since = None
        self._pre_peak = 0
        return ended
