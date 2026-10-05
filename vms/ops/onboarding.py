"""Estado del onboarding por usuario en `<datos>/ops/onboarding.json` (CONTRATO §18.14).

Solo guarda el progreso del asistente de primer uso y qué recorridos y pistas ha visto cada usuario: nada
de datos personales más allá del nombre de usuario que ya está en `users.json`.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from vms.core.atomic import atomic_write_text

from .models import OnboardingState

log = logging.getLogger("vms.ops.onboarding")

KNOWN_TOURS = ("panel", "monitores", "reproduccion", "analitica", "estado")


class OnboardingStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("onboarding.json ilegible (%s): se empieza de cero", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, username: str) -> OnboardingState:
        raw = self._read().get(username.lower())
        if isinstance(raw, dict):
            try:
                return OnboardingState.model_validate({**raw, "username": username})
            except ValueError:
                log.warning("Estado de onboarding de «%s» no válido: se reinicia", username)
        return OnboardingState(username=username)

    def put(self, state: OnboardingState) -> OnboardingState:
        clean = state.model_copy(update={
            "tours_seen": sorted(set(t for t in state.tours_seen if len(t) <= 40))[:50],
            "dismissed_hints": sorted(set(h for h in state.dismissed_hints if len(h) <= 60))[:200],
            "wizard_step": max(0, min(20, state.wizard_step))})
        with self._lock:
            data = self._read()
            data[state.username.lower()] = clean.model_dump(exclude={"username"})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=1))
        return clean

    def forget(self, username: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(username.lower(), None) is not None:
                atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=1))
