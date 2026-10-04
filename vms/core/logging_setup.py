"""Registros rotativos en archivo + consola, con credenciales ocultas.

La ocultación se hace sobre el texto FINAL formateado (mensaje + traza de excepción), así
también se limpian contraseñas que aparezcan dentro de excepciones de terceros.
Uvicorn debe arrancarse con log_config=None para que use estos manejadores.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .rtsp import redact

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RedactFilter(logging.Filter):
    """Para manejadores ajenos (p. ej. capturas de pytest): limpia el mensaje antes de formatear."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # argumentos que no casan con el formato: se deja como está
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


_installed: list[logging.Handler] = []


def setup_logging(logs_dir: Path | None, level: str | int = "INFO", filename: str = "vms.log",
                  console: bool = True) -> None:
    """Configura el registro raíz. Idempotente: llamarlo dos veces no duplica manejadores."""
    root = logging.getLogger()
    for h in _installed:
        root.removeHandler(h)
        h.close()
    _installed.clear()
    root.setLevel(level if isinstance(level, int) else getattr(logging, str(level).upper(), logging.INFO))
    fmt = RedactingFormatter(FORMAT)
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(logs_dir / filename, maxBytes=5_000_000,
                                                  backupCount=10, encoding="utf-8")
        fh.setFormatter(fmt)
        _installed.append(fh)
    if console and sys.stderr is not None:  # en un .exe sin consola sys.stderr es None
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        _installed.append(sh)
    for h in _installed:
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "zeep", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- registro de accesos
AUDIT_LOGGER = "vms.audit"
_audit_handlers: list[logging.Handler] = []


def setup_audit_log(logs_dir: Path, filename: str = "audit.log", *, max_bytes: int = 10_000_000,
                    backups: int = 20) -> Path:
    """Registro de accesos a imágenes (RGPD art. 32): quién vio o descargó qué grabación y cuándo.

    Va a su propio archivo rotativo (además del registro general). Idempotente."""
    log = logging.getLogger(AUDIT_LOGGER)
    for h in _audit_handlers:
        log.removeHandler(h)
        h.close()
    _audit_handlers.clear()
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / filename
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    fh.setFormatter(RedactingFormatter("%(asctime)s %(message)s"))
    fh.setLevel(logging.INFO)
    log.addHandler(fh)
    log.setLevel(logging.INFO)
    _audit_handlers.append(fh)
    return path
