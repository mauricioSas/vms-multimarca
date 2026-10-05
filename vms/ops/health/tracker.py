"""Histéresis del estado de salud por cámara (CONTRATO §18.2).

Una comprobación suelta no cambia el estado: hacen falta `hysteresis` comprobaciones seguidas con el
mismo estado (por defecto 3). Así un objeto que tapa la cámara un momento, un destello o un fotograma
corrupto no generan avisos, y el estado tampoco «parpadea» al recuperarse.

Excepción: desde «desconocido» (recién arrancado o sin referencia) a «correcto» se pasa en el acto,
porque no hay nada que avisar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ..models import CameraHealth, HealthCause, HealthCheck

Status = Literal["ok", "warning", "critical", "unknown"]


@dataclass
class _State:
    status: Status = "unknown"
    score: int | None = None
    causes: list[HealthCause] = field(default_factory=list)
    since: datetime | None = None
    candidate: Status | None = None
    candidate_count: int = 0
    last_check: HealthCheck | None = None


@dataclass(frozen=True)
class Transition:
    camera_id: str
    previous: Status
    current: Status
    health: CameraHealth


class HealthTracker:
    def __init__(self, hysteresis: int = 3) -> None:
        self.hysteresis = max(1, int(hysteresis))
        self._states: dict[str, _State] = {}

    def restore(self, camera_id: str, health: CameraHealth) -> None:
        """Estado consolidado guardado (tras reiniciar el backend no se vuelve a «desconocido»)."""
        self._states[camera_id] = _State(status=health.status, score=health.score, causes=list(health.causes),
                                         since=health.since, last_check=health.last_check)

    def forget(self, camera_id: str) -> None:
        self._states.pop(camera_id, None)

    def get(self, camera_id: str) -> CameraHealth:
        st = self._states.get(camera_id) or _State()
        return CameraHealth(camera_id=camera_id, score=st.score, status=st.status, causes=list(st.causes),
                            since=st.since, last_check=st.last_check)

    def update(self, check: HealthCheck) -> Transition | None:
        """Añade una comprobación. Devuelve la transición si el estado consolidado cambia."""
        st = self._states.setdefault(check.camera_id, _State())
        st.last_check = check
        new: Status = check.status
        if new == st.status:
            st.candidate, st.candidate_count = None, 0
            st.score, st.causes = check.score, list(check.causes)   # mismo estado: se refrescan los datos
            return None
        if st.candidate == new:
            st.candidate_count += 1
        else:
            st.candidate, st.candidate_count = new, 1
        immediate = st.status == "unknown" and new == "ok"
        if not immediate and st.candidate_count < self.hysteresis:
            return None
        previous = st.status
        st.status, st.score, st.causes, st.since = new, check.score, list(check.causes), check.at
        st.candidate, st.candidate_count = None, 0
        return Transition(camera_id=check.camera_id, previous=previous, current=new, health=self.get(check.camera_id))
