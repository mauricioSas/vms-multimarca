"""Registro en archivo rotativo, ocultando credenciales de las URL RTSP."""
from __future__ import annotations

import logging
import logging.handlers
import sys

from .paths import logs_dir
from .rtsp import redact


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(logs_dir() / "vms.log", maxBytes=2_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(RedactFilter())
    root.addHandler(fh)
    if sys.stderr is not None:  # en el .exe sin consola sys.stderr es None
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(RedactFilter())
        root.addHandler(sh)
