"""Health check de una versión nueva (PLAN-V2 §2.5 paso 7).

`GET /api/internal/health/deep` con el token interno (`x-vms-internal-token`). Se exige todo:
- `status != down` y la versión activa es la esperada;
- `engine.running` y `engine.api_ok`;
- cámaras grabando ≥ las que grababan antes − 1;
- si la analítica está activa, su `status.json` tiene menos de 30 s y `running`;
- el visor no cuenta.
Hace falta pasarlo dos veces seguidas (para no dar por buena una versión que arranca y cae).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import urllib3

log = logging.getLogger("vms_updater.health")

DEEP_PATH = "/api/internal/health/deep"
INTERNAL_HEADER = "x-vms-internal-token"


@dataclass
class HealthResult:
    ok: bool
    reason_es: str
    sample: dict[str, Any] | None = None


def read_internal_token(secrets_dir: Path) -> str:
    """Token interno del backend. En Windows va cifrado con DPAPI de máquina (B1, `vms.core.winsec`);
    el actualizador es LocalSystem y puede descifrarlo. Si el módulo de B1 no está, se lee en claro."""
    path = Path(secrets_dir) / "internal.token"
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    try:  # pragma: no cover - depende de B1 y de Windows
        import importlib
        winsec = importlib.import_module("vms.core.winsec")
        unprotect = getattr(winsec, "unprotect_file_bytes", None)
        if callable(unprotect):
            raw = unprotect(raw)
    except Exception:  # noqa: BLE001
        pass
    return raw.decode("utf-8", errors="ignore").strip()


def evaluate(sample: dict[str, Any] | None, *, expected_version: str | None, min_recording: int | None) -> HealthResult:
    if sample is None:
        return HealthResult(False, "el backend no responde", None)
    if sample.get("status") == "down":
        return HealthResult(False, "el backend dice que está caído (status=down)", sample)
    version = sample.get("release") or sample.get("version")
    if expected_version is not None and version != expected_version:
        return HealthResult(False, f"la versión en marcha es {version!r}, se esperaba {expected_version}", sample)
    raw_engine = sample.get("engine")
    engine: dict[str, Any] = raw_engine if isinstance(raw_engine, dict) else {}
    if not engine.get("running") or not engine.get("api_ok"):
        return HealthResult(False, "el motor de vídeo no está en marcha", sample)
    if min_recording is not None and min_recording > 0:
        rec = int(sample.get("cameras_recording") or 0)
        if rec < min_recording - 1:
            return HealthResult(False, f"graban {rec} cámaras y antes grababan {min_recording}", sample)
    raw_an = sample.get("analytics")
    an: dict[str, Any] = raw_an if isinstance(raw_an, dict) else {}
    if an.get("enabled"):
        age = an.get("age_s")
        if not an.get("running") or age is None or float(age) >= 30:
            return HealthResult(False, "la analítica no tiene estado reciente", sample)
    return HealthResult(True, "ok", sample)


class DeepHealthChecker:
    def __init__(self, base_url: str, token_reader: Callable[[], str], *, timeout_s: float = 120.0,
                 interval_s: float = 2.0, stable_passes: int = 2, request_timeout_s: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_reader = token_reader
        self.timeout_s = timeout_s
        self.interval_s = interval_s
        self.stable_passes = stable_passes
        self.request_timeout_s = request_timeout_s
        self.sleep = sleep
        self.monotonic = monotonic
        self._pool = urllib3.PoolManager()

    def sample(self) -> dict[str, Any] | None:
        try:
            r = self._pool.request("GET", self.base_url + DEEP_PATH,
                                   headers={INTERNAL_HEADER: self.token_reader()},
                                   timeout=urllib3.Timeout(self.request_timeout_s), retries=False)
        except urllib3.exceptions.HTTPError:
            return None
        if r.status != 200:
            log.debug("health deep → HTTP %s", r.status)
            return None
        try:
            data = json.loads(r.data)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def recording_now(self) -> int | None:
        s = self.sample()
        if s is None:
            return None
        try:
            return int(s.get("cameras_recording") or 0)
        except (TypeError, ValueError):
            return None

    def wait_healthy(self, expected_version: str | None, min_recording: int | None) -> HealthResult:
        deadline = self.monotonic() + self.timeout_s
        passes = 0
        last = HealthResult(False, "sin respuesta del backend")
        while True:
            last = evaluate(self.sample(), expected_version=expected_version, min_recording=min_recording)
            passes = passes + 1 if last.ok else 0
            if passes >= self.stable_passes:
                return last
            if self.monotonic() >= deadline:
                return HealthResult(False, f"health check fallido tras {self.timeout_s:.0f} s: {last.reason_es}",
                                    last.sample)
            self.sleep(self.interval_s)
