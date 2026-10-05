"""Motor como servicio aparte, sin Windows: `python -m vms engine-config` y `python -m vms engine-run`.

- `engine-config` escribe `mediamtx.yml` completo (rutas con sus contraseñas, modo attach) a partir de
  `config.json` y del almacén de credenciales. Lo ejecutan `vmsctl services install` y el actualizador antes
  de arrancar los servicios, con permisos de administrador (lee `secrets\\`).
- `engine-run` hace en desarrollo (macOS/Linux) lo que en Windows hace `vmsctl run --service VMSEngine`:
  lanza MediaMTX con ese YAML, copia su salida **sin credenciales** a `logs/engine.log` (rotación
  10 × 10 MB) y lo relanza con espera creciente si cae. Así se prueba el modo attach de punta a punta.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from vms.core.config_store import ConfigStore
from vms.core.credentials import CredentialStore
from vms.core.logging_setup import RedactingFormatter
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.core.sources import build_camera_sources

from .mtx_config import attach_config, path_configs, render_attach, write_if_changed
from .process import BACKOFF, STABLE_AFTER

log = logging.getLogger("vms.engine.service")

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 9


def write_engine_config(settings: VmsSettings, paths: AppPaths) -> tuple[Path, int, bool]:
    """(archivo, rutas, ¿cambió?). Todas las cámaras habilitadas, sin pausas (no hay estado previo)."""
    from vms.core.mtx_auth import MtxCredentials

    paths.ensure()
    cfg, warning = ConfigStore(paths.config_file).load()
    if warning:
        log.warning("%s", warning)
    creds = CredentialStore.create(settings.credential_backend, paths.secrets_dir,
                                   settings.secret_key.get_secret_value() if settings.secret_key else None)
    routes: dict[str, dict[str, object]] = {}
    for src in build_camera_sources(cfg, creds):
        routes.update(path_configs(src))
    recordings_dir = cfg.settings.recording.recordings_dir or str(paths.recordings_dir)
    Path(recordings_dir).mkdir(parents=True, exist_ok=True)
    doc = attach_config(settings, cfg.settings.recording, cfg.settings.retention, recordings_dir, routes,
                        creds=MtxCredentials.from_internal_token(settings.ensure_internal_token()))
    file = paths.mediamtx_dir / "mediamtx.yml"
    changed = write_if_changed(file, render_attach(doc))
    return file, len(routes), changed


def _engine_log(paths: AppPaths) -> logging.Logger:
    """Registro `logs/engine.log` sin credenciales y con el texto tal cual (logtail lee la fecha de MediaMTX)."""
    logger = logging.getLogger("vms.engine.service.output")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir / "engine.log", maxBytes=LOG_MAX_BYTES,
                                              backupCount=LOG_BACKUPS, encoding="utf-8")
    fh.setFormatter(RedactingFormatter("%(message)s"))
    logger.addHandler(fh)
    return logger


def _pump(stream: IO[bytes], out: logging.Logger) -> None:
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            out.info("%s", line)


def _spawn(exe: Path, yml: Path, cwd: Path) -> subprocess.Popen[bytes]:
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":  # pragma: no cover - en Windows esto lo hace vmsctl
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([str(exe), str(yml)], cwd=str(cwd), stdin=subprocess.DEVNULL,  # type: ignore[call-overload]
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)


def run_engine(paths: AppPaths, exe: Path, stop: threading.Event, *,
               backoff: tuple[float, ...] = BACKOFF, grace: float = 10.0) -> int:
    """Bucle del motor (hasta `stop`). Devuelve 0 al parar a petición, 2 si falta el ejecutable."""
    if not exe.is_file():
        log.error("No existe el ejecutable de MediaMTX: %s", exe)
        return 2
    out = _engine_log(paths)
    yml = paths.mediamtx_dir / "mediamtx.yml"
    attempt = 0
    while not stop.is_set():
        said = False
        while not yml.is_file():
            if not said:
                out.info("%s [engine-run] espero a que exista %s", time.strftime("%Y-%m-%dT%H:%M:%S"), yml)
                said = True
            if stop.wait(1.0):
                return 0
        proc = _spawn(exe, yml, paths.mediamtx_dir)
        assert proc.stdout is not None
        reader = threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True, name="engine-output")
        reader.start()
        started = time.monotonic()
        out.info("%s [engine-run] MediaMTX en marcha (pid %s)", time.strftime("%Y-%m-%dT%H:%M:%S"), proc.pid)
        while proc.poll() is None:
            if stop.wait(0.2):
                proc.terminate()   # SIGTERM: MediaMTX cierra los segmentos en curso
                try:
                    proc.wait(grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                reader.join(2.0)
                return 0
        reader.join(2.0)
        if time.monotonic() - started > STABLE_AFTER:
            attempt = 0
        delay = backoff[min(attempt, len(backoff) - 1)]
        attempt += 1
        out.info("%s [engine-run] MediaMTX terminó (código %s); se relanza en %.0f s",
                 time.strftime("%Y-%m-%dT%H:%M:%S"), proc.returncode, delay)
        if stop.wait(delay):
            return 0
    return 0


def install_stop_handlers(stop: threading.Event) -> None:
    """Ctrl+C / SIGTERM y, si `VMS_STOP_ON_STDIN_EOF=1` (vmsctl), el cierre de la entrada estándar."""
    def handler(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)
    if os.environ.get("VMS_STOP_ON_STDIN_EOF") == "1":
        from vms.core.stdin_stop import private_stdin

        stdin = private_stdin()   # en Windows, separada del STD_INPUT_HANDLE (ver vms/core/stdin_stop.py)

        def watch() -> None:
            try:
                while stdin is not None and stdin.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            stop.set()

        threading.Thread(target=watch, daemon=True, name="stdin-eof").start()
