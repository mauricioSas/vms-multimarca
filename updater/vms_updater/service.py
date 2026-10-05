"""Montaje del actualizador desde el entorno y bucle del servicio `VMSUpdater`.

Variables (las fija el instalador en el entorno del servicio o en `.env`):

| Variable | Por defecto | Qué es |
|---|---|---|
| `VMS_UPDATE_SOURCE` | — (sin fuente = sin actualizaciones) | `https://…/<cliente>/`, `http://…/` o `file:///…` (CONTRATO §15.1) |
| `VMS_UPDATE_MODE` | según el esquema (`file` → `offline`) | qué `root` de confianza usar |
| `VMS_UPDATE_TOKEN` | `secrets\\update.token` | token de la sede para el Worker (no es el del latido) |
| `VMS_BACKEND_URL` | `http://127.0.0.1:8600` | para el health check |
| `VMS_UPDATER_HEALTH_TIMEOUT` | 120 | segundos |
| `VMS_UPDATER_CHECK_HOURS` | 6 | cada cuánto se comprueba (±30 min al azar) |
| `VMS_UPDATER_VMSCTL` | `<ranura>\\vmsctl.exe` | orden de `vmsctl` (en pruebas, el doble) |
| `VMS_UPDATER_REQUIRE_AUTHENTICODE` | 1 si el `root` no es de desarrollo | exigir firma Authenticode |
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .authenticode import system_verifier
from .client import TufClient, parse_source
from .engine import RELOAD_EXIT_CODE, Engine, EngineDeps, Outcome, current_slot
from .health import DeepHealthChecker, read_internal_token
from .journal import env_fault_hook
from .layout import Layout
from .migrate import SubprocessMigrator
from .services import VmsctlServices
from .state_files import in_window, next_window_start, read_directive
from .system import RealSystem

log = logging.getLogger("vms_updater.service")


def read_update_token(layout: Layout) -> str | None:
    env = os.environ.get("VMS_UPDATE_TOKEN", "").strip()
    if env:
        return env
    path = layout.secrets_dir / "update.token"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:  # pragma: no cover - DPAPI de B1 en Windows
        from vms.core import winsec  # type: ignore[attr-defined]
        unprotect = getattr(winsec, "unprotect_file_bytes", None)
        if callable(unprotect):
            raw = unprotect(raw)
    except Exception:  # noqa: BLE001
        pass
    return raw.decode("utf-8", errors="ignore").strip() or None


def root_is_dev(root_bytes: bytes) -> bool:
    try:
        signed = json.loads(root_bytes).get("signed", {})
    except (ValueError, AttributeError):
        return False
    return signed.get("x-vms-env") == "dev"


def build_engine(layout: Layout | None = None) -> Engine:
    layout = (layout or Layout.from_env()).ensure()
    source_url = os.environ.get("VMS_UPDATE_SOURCE", "").strip()
    source = parse_source(source_url, token=read_update_token(layout),
                          mode=os.environ.get("VMS_UPDATE_MODE") or None) if source_url else None
    trusted = b""
    if source is not None:
        trusted_file = layout.trusted_root_file(source.mode)
        try:
            trusted = trusted_file.read_bytes()
        except OSError as exc:
            raise SystemExit(f"Falta el root de confianza {trusted_file}: reinstala con el instalador") from exc
    dev = root_is_dev(trusted) if trusted else True
    require_ac = os.environ.get("VMS_UPDATER_REQUIRE_AUTHENTICODE", "0" if dev else "1") == "1"

    def client_factory() -> TufClient:
        if source is None:
            from .client import UpdateSourceError
            raise UpdateSourceError("No hay fuente de actualizaciones configurada (VMS_UPDATE_SOURCE)")
        return TufClient(source, metadata_dir=layout.tuf_metadata_dir, targets_dir=layout.tuf_targets_dir,
                         trusted_root=trusted)

    health = DeepHealthChecker(os.environ.get("VMS_BACKEND_URL", "http://127.0.0.1:8600"),
                               lambda: read_internal_token(layout.secrets_dir),
                               timeout_s=float(os.environ.get("VMS_UPDATER_HEALTH_TIMEOUT", "120")),
                               interval_s=float(os.environ.get("VMS_UPDATER_HEALTH_INTERVAL", "2")))
    slot = current_slot()
    services = VmsctlServices.from_env(layout.slot_dir(slot) if slot else None)
    deps = EngineDeps(layout=layout, client_factory=client_factory, services=services, health=health,
                      system=RealSystem(),
                      migrate=SubprocessMigrator(versions_dir=layout.versions_dir, data_dir=layout.data),
                      verifier=system_verifier(), dev_root=dev, require_authenticode=require_ac,
                      fault_hook=env_fault_hook(), slot=slot)
    engine = Engine(deps)
    hook = deps.fault_hook
    if hook is not None:
        hook.announce = engine.announce_pause  # type: ignore[attr-defined]
    return engine


def next_check_delay(hours: float, rng: random.Random) -> float:
    return max(60.0, hours * 3600 + rng.uniform(-1800, 1800))


def run_service(engine: Engine, stop: threading.Event | None = None, *, hours: float | None = None,
                poll_s: float = 30.0, rng: random.Random | None = None) -> int:
    """Bucle del servicio. Devuelve el código de salida (3 = relanzar con la ranura nueva)."""
    from .control_pipe import make_server

    stop = stop or threading.Event()
    rng = rng or random.Random()
    hours = hours if hours is not None else float(os.environ.get("VMS_UPDATER_CHECK_HOURS", "6"))
    out = engine.startup()
    if out is not None and out.result == "restart_updater":
        return RELOAD_EXIT_CODE
    server = make_server(engine)
    server.start()
    next_check = time.monotonic()          # comprueba al arrancar
    seen_check: str | None = None
    try:
        while not stop.is_set():
            res: Outcome | None = engine.handle_rollback_request() or engine.handle_directive_rollback()
            d = read_directive(engine.layout.directive_file)
            if d is not None and d.check and d.received != seen_check:
                seen_check = d.received
                next_check = time.monotonic()
            if time.monotonic() >= next_check:
                res = engine.check()
                if res.result == "restart_updater":
                    return RELOAD_EXIT_CODE
                delay = next_check_delay(hours, rng)
                if res.result == "waiting_window":
                    cfg = engine.config()
                    now = engine.d.now_local()
                    if not in_window(now, cfg.window):
                        delay = min(delay, (next_window_start(now, cfg.window) - now).total_seconds()
                                    + rng.uniform(0, 600))
                    else:
                        delay = min(delay, 600)
                next_check = time.monotonic() + delay
            stop.wait(poll_s)
        return 0
    finally:
        server.stop()


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: Any) -> None:
        log.info("Señal %s: parando el actualizador", signum)
        stop.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def setup_logging(layout: Layout, verbose: bool = False) -> None:
    from logging.handlers import RotatingFileHandler

    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(layout.logs_dir / "updater.log", maxBytes=10 * 1024 * 1024, backupCount=10,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("tuf").setLevel(logging.WARNING)
