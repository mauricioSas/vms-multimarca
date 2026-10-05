"""Control de servicios por `vmsctl` (CONTRATO §14), siempre con `--json` y códigos de salida estables.

El actualizador nunca analiza texto localizado de Windows: solo el JSON de `vmsctl`. Mientras B1 no
entregue `vmsctl.exe`, las pruebas usan un doble con la misma CLI (`tests/updater/doubles/vmsctl_double.py`).
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("vms_updater.services")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PORT_IN_USE = 10
EXIT_NO_PERMISSION = 11
EXIT_HEALTH_FAILED = 12
EXIT_WINDOWS_ERROR = 20


class ServiceError(Exception):
    def __init__(self, message_es: str, *, code: int | None = None, error_code: str = "") -> None:
        super().__init__(message_es)
        self.message_es = message_es
        self.code = code
        self.error_code = error_code


class ServiceControl(Protocol):
    def stop(self, services: Sequence[str]) -> None: ...
    def start(self, services: Sequence[str]) -> None: ...
    def restart(self, services: Sequence[str]) -> None: ...


@dataclass
class VmsctlServices:
    """Habla con `vmsctl services start|stop|restart --only … --json`."""

    command: list[str]
    timeout_s: float = 180.0

    @classmethod
    def from_env(cls, slot_dir: Path | None = None) -> "VmsctlServices":
        raw = os.environ.get("VMS_UPDATER_VMSCTL", "").strip()
        if raw:
            cmd = shlex.split(raw, posix=sys.platform != "win32")
            # en Windows shlex (posix=False) conserva las comillas de cada palabra
            cmd = [c[1:-1] if len(c) >= 2 and c[0] == c[-1] == '"' else c for c in cmd]
        else:
            exe = "vmsctl.exe" if sys.platform == "win32" else "vmsctl"
            base = slot_dir if slot_dir is not None else Path(sys.executable).resolve().parent.parent
            cmd = [str(base / exe)]
        return cls(cmd)

    def _run(self, args: list[str]) -> dict[str, Any]:
        full = [*self.command, *args, "--json"]
        log.info("vmsctl %s", " ".join(args))
        try:
            proc = subprocess.run(full, capture_output=True, timeout=self.timeout_s, check=False,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as exc:
            raise ServiceError(f"No se encontró vmsctl ({self.command[0]})", error_code="vmsctl_missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise ServiceError(f"vmsctl {' '.join(args)} no respondió en {self.timeout_s:.0f} s",
                               error_code="vmsctl_timeout") from exc
        out = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
        data: dict[str, Any] = {}
        if out:
            try:
                parsed = json.loads(out[-1])
                if isinstance(parsed, dict):
                    data = parsed
            except ValueError:
                pass
        if proc.returncode != EXIT_OK or data.get("ok") is False:
            raw_err = data.get("error")
            err: dict[str, Any] = raw_err if isinstance(raw_err, dict) else {}
            msg = str(err.get("message_es") or f"vmsctl {' '.join(args)} falló (código {proc.returncode})")
            raise ServiceError(msg, code=proc.returncode, error_code=str(err.get("code") or ""))
        return data

    def stop(self, services: Sequence[str]) -> None:
        if services:
            self._run(["services", "stop", "--only", ",".join(services)])

    def start(self, services: Sequence[str]) -> None:
        if services:
            self._run(["services", "start", "--only", ",".join(services)])

    def restart(self, services: Sequence[str]) -> None:
        if services:
            self._run(["services", "restart", "--only", ",".join(services)])
