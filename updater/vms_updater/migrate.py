"""Migraciones al aplicar una versión (PLAN-V2 §2.5 paso 6.3-6.4 y §2.8).

Se ejecutan **con el código de la versión nueva** (su runtime y su `app\\`), nunca con el del actualizador:
`versions\\<X>\\runtime\\python.exe -m vms.core.config_migrations migrate --data-dir <datos>`
(funciones puras, escritura atómica; idempotente: con la versión al día no hace nada). En el panel
central, además, `-m vms.db.migrate` (PostgreSQL con expand/contract).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger("vms_updater.migrate")


class MigrationError(Exception):
    def __init__(self, message_es: str) -> None:
        super().__init__(message_es)
        self.message_es = message_es


class SubprocessMigrator:
    def __init__(self, *, versions_dir: Path, data_dir: Path, timeout_s: float = 300.0) -> None:
        self.versions_dir = Path(versions_dir)
        self.data_dir = Path(data_dir)
        self.timeout_s = timeout_s

    def _python(self, version: str) -> tuple[list[str], dict[str, str]]:
        vdir = self.versions_dir / version
        exe = vdir / "runtime" / ("python.exe" if sys.platform == "win32" else "bin/python3")
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["VMS_DATA_DIR"] = str(self.data_dir)
        if exe.is_file():
            return [str(exe)], env
        # Desarrollo (sin runtime embebido): el intérprete actual con el código de la versión delante.
        app = vdir / "app"
        env["PYTHONPATH"] = os.pathsep.join([str(app), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        return [sys.executable], env

    def _run(self, version: str, args: Sequence[str]) -> None:
        cmd, env = self._python(version)
        full = [*cmd, *args]
        try:
            proc = subprocess.run(full, capture_output=True, timeout=self.timeout_s, env=env, check=False,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MigrationError(f"No se pudo ejecutar la migración ({type(exc).__name__})") from exc
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
            raise MigrationError("La migración falló: " + " | ".join(tail)[:400])

    def __call__(self, version: str, *, central: bool = False) -> None:
        self._run(version, ["-m", "vms.core.config_migrations", "migrate", "--data-dir", str(self.data_dir)])
        if central:
            self._run(version, ["-m", "vms.db.migrate"])
