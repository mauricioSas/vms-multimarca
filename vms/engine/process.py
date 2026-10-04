"""Supervisor del proceso MediaMTX.

- Lanza el binario con la configuración generada y reenvía su salida, línea a línea, al
  logger «vms.engine.mediamtx» (así pasa por la ocultación de credenciales).
- Si el proceso termina sin que se haya pedido, lo relanza con espera creciente
  (1, 2, 5, 10, 30 s) y avisa al motor para que vuelva a registrar las rutas.
- Evita procesos huérfanos: archivo pid (se detiene un MediaMTX nuestro que quedara de un
  arranque anterior), PR_SET_PDEATHSIG en Linux y Job Object «kill on close» en Windows.
- En Windows el proceso se crea sin ventana (CREATE_NO_WINDOW).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from vms.core.errors import EngineUnavailable

log = logging.getLogger("vms.engine.process")

BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
STABLE_AFTER = 60.0  # si aguanta este tiempo en marcha, la espera vuelve a empezar desde 1 s

LineHandler = Callable[[str], None]
StartHook = Callable[[], Awaitable[None]]


def port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- huérfanos
def _linux_pdeathsig() -> None:  # pragma: no cover - solo Linux (se ejecuta en el hijo)
    import ctypes
    import signal
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


class _WindowsJob:  # pragma: no cover - solo Windows
    """Job Object con JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: si el backend muere, MediaMTX también."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")

    def assign(self, pid: int) -> None:
        handle = self._k32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | TERMINATE
        if not handle:
            raise OSError(self._ctypes.get_last_error(), "OpenProcess")
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, handle):
                raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject")
        finally:
            self._k32.CloseHandle(handle)


def kill_stale(pid_file: Path, exe: Path) -> None:
    """Detiene un MediaMTX nuestro que haya quedado de un arranque anterior (backend matado)."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil es dependencia del extra [vms]
        return
    try:
        proc = psutil.Process(pid)
        same = Path(proc.exe()).resolve() == exe.resolve()
    except (psutil.Error, OSError):
        return
    if not same:
        return
    log.warning("Se detiene un MediaMTX que quedó en marcha de un arranque anterior (pid %s)", pid)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except psutil.TimeoutExpired:
        proc.kill()
    except psutil.Error as exc:
        log.warning("No se pudo detener el MediaMTX anterior: %s", exc)


# --------------------------------------------------------------------------- supervisor
class MediaMtxProcess:
    def __init__(self, exe: Path, config_file: Path, *, workdir: Path, api_host: str, api_port: int,
                 on_line: LineHandler | None = None, on_restart: StartHook | None = None,
                 before_spawn: Callable[[], None] | None = None, backoff: tuple[float, ...] = BACKOFF,
                 api_auth: tuple[str, str] | None = None) -> None:
        self.exe = Path(exe)
        self.api_auth = api_auth
        self.config_file = Path(config_file)
        self.workdir = Path(workdir)
        self.api_host, self.api_port = api_host, api_port
        self.on_line = on_line
        self.on_restart = on_restart
        self.before_spawn = before_spawn
        self.backoff = backoff
        self.pid_file = self.workdir / "mediamtx.pid"
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.started_at: datetime | None = None
        self.last_error = ""
        self.recent_lines: deque[str] = deque(maxlen=40)
        self._stopping = False
        self._monitor: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._job: Any = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.running and self.proc else None

    async def _spawn(self) -> None:
        if self.before_spawn is not None:
            # p. ej. regenerar mediamtx.yml con los ajustes actuales. Nunca con el proceso en marcha:
            # MediaMTX vigila su archivo y al recargarlo borraría las rutas registradas por la API.
            await asyncio.to_thread(self.before_spawn)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":  # pragma: no cover - solo Windows
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # Ctrl+C en la consola no le llega: lo paramos nosotros
            if sys.platform.startswith("linux"):
                kwargs["preexec_fn"] = _linux_pdeathsig
        self.proc = await asyncio.create_subprocess_exec(
            str(self.exe), str(self.config_file), cwd=str(self.workdir),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **kwargs)
        self.started_at = datetime.now(timezone.utc)
        if sys.platform == "win32":  # pragma: no cover - solo Windows
            try:
                if self._job is None:
                    self._job = _WindowsJob()
                self._job.assign(self.proc.pid)
            except OSError as exc:
                log.warning("No se pudo vincular MediaMTX al backend (Job Object): %s", exc)
        try:
            self.pid_file.write_text(str(self.proc.pid), encoding="utf-8")
        except OSError as exc:
            log.warning("No se pudo escribir %s: %s", self.pid_file, exc)
        self._reader = asyncio.create_task(self._read_output(self.proc), name="mediamtx-stdout")
        log.info("MediaMTX arrancado (pid %s)", self.proc.pid)

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # línea más larga que el búfer: se descarta
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            self.recent_lines.append(line)
            if self.on_line is not None:
                try:
                    self.on_line(line)
                except Exception:  # noqa: BLE001 - un fallo al interpretar no debe cortar la lectura
                    log.exception("Error procesando una línea de MediaMTX")

    async def start(self, ready_timeout: float = 20.0) -> None:
        if self.running:
            return
        if not self.exe.is_file():
            raise EngineUnavailable(f"No se encuentra el ejecutable de MediaMTX en {self.exe}. "
                                    "Revisa la instalación o VMS_MEDIAMTX_BIN.")
        self.workdir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(kill_stale, self.pid_file, self.exe)
        if port_in_use(self.api_host, self.api_port):
            raise EngineUnavailable(
                f"El puerto {self.api_host}:{self.api_port} de la API de MediaMTX ya está ocupado. "
                "¿Hay otro VMS o MediaMTX en marcha? Ciérralo o cambia VMS_MTX_API_ADDRESS.")
        self._stopping = False
        await self._spawn()
        await self.wait_api(ready_timeout)
        self._monitor = asyncio.create_task(self._watch(), name="mediamtx-supervisor")

    async def wait_api(self, timeout: float) -> None:
        import httpx

        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=1.0, auth=self.api_auth) as client:
            while time.monotonic() < deadline:
                if not self.running:
                    # Damos un instante al lector para recoger el motivo
                    await asyncio.sleep(0.2)
                    reason = self._failure_reason()
                    self.last_error = f"MediaMTX terminó al arrancar: {reason}"
                    raise EngineUnavailable(self.last_error)
                try:
                    r = await client.get(f"http://{self.api_host}:{self.api_port}/v3/info")
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        self.last_error = "La API de MediaMTX no respondió a tiempo"
        raise EngineUnavailable(self.last_error)

    def _failure_reason(self) -> str:
        """Último error del propio MediaMTX (no de una cámara concreta), o «sin mensaje de error»."""
        for ln in reversed(self.recent_lines):
            if " ERR " in ln:
                msg = ln.split(" ERR ", 1)[1]
                if not msg.startswith("[path "):
                    return msg[:300]
        return "sin mensaje de error (cierre inesperado)"

    async def _watch(self) -> None:
        attempt = 0
        while not self._stopping:
            proc = self.proc
            if proc is None:
                return
            rc = await proc.wait()
            if self._reader is not None:
                try:
                    await asyncio.wait_for(self._reader, 2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            if self._stopping:
                return
            alive = (datetime.now(timezone.utc) - self.started_at).total_seconds() if self.started_at else 0
            if alive > STABLE_AFTER:
                attempt = 0
            self.last_error = f"MediaMTX se detuvo (código {rc}): {self._failure_reason()}"
            delay = self.backoff[min(attempt, len(self.backoff) - 1)]
            log.error("%s. Se relanza en %.0f s", self.last_error, delay)
            attempt += 1
            await asyncio.sleep(delay)
            if self._stopping:
                return
            try:
                if port_in_use(self.api_host, self.api_port):
                    raise EngineUnavailable(f"el puerto {self.api_port} de la API está ocupado por otro programa")
                await self._spawn()
                self.restarts += 1
                await self.wait_api(20.0)
                log.info("MediaMTX relanzado (reinicio n.º %d)", self.restarts)
                if self.on_restart is not None:
                    await self.on_restart()
            except EngineUnavailable as exc:
                self.last_error = exc.message
                log.error("No se pudo relanzar MediaMTX: %s", exc.message)
                if self.proc is not None and self.proc.returncode is None:
                    await self._terminate(self.proc)
            except Exception:  # noqa: BLE001 - el supervisor no puede morir
                log.exception("Error inesperado relanzando MediaMTX")

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except asyncio.TimeoutError:
            log.warning("MediaMTX no se detuvo a tiempo; se fuerza el cierre")
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

    async def stop(self) -> None:
        self._stopping = True
        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
            self._monitor = None
        if self.proc is not None:
            await self._terminate(self.proc)
            log.info("MediaMTX detenido")
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, 2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
            self._reader = None
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("No se pudo borrar %s: %s", self.pid_file, exc)

    def kill_for_tests(self) -> None:
        """Mata el proceso sin avisar (para probar el relanzamiento)."""
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()


__all__ = ["MediaMtxProcess", "port_in_use", "kill_stale"]
