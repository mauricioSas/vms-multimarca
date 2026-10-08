"""MediaMtxEngine: implementación de vms.core.interfaces.Engine sobre MediaMTX (CONTRATO §4, §5.2 y §13.10).

Dos modos (`VMS_ENGINE_MODE`):
- `child` (desarrollo en macOS/Linux y v1): el backend lanza y supervisa MediaMTX y registra las rutas por
  su API (las contraseñas solo viven en la memoria de MediaMTX).
- `attach` (Windows, motor como servicio `VMSEngine`): MediaMTX lo lanza `vmsctl run`; el backend **no**
  lo lanza: escribe `mediamtx.yml` completo (rutas incluidas) con `atomic_write` en cada cambio y MediaMTX
  lo recarga solo (solo reinicia las rutas que cambian). Así se graba aunque el backend esté caído o
  reiniciándose. La API se usa solo para leer estado y como proxy WHEP/reproducción; se vigila cada 2 s.
  Los 401 se detectan siguiendo `logs/engine.log` (ya sin credenciales, `vms.engine.logtail`). Si la API no
  responde, `status()` dice por qué (puerto ocupado y por qué programa, configuración rechazada, caídas
  seguidas, servicio parado…) con engine.log y `logs/status-VMSEngine.json` (`vms.engine.problem`).

Responsabilidades comunes:
- Generar mediamtx.yml (sin credenciales) y supervisar el proceso (vms.engine.process).
- Registrar por la API de control una ruta «<cámara>/main» (grabación 24/7) y «<cámara>/sub»
  (subflujo bajo demanda) por cámara; sincronizar altas, cambios y bajas sin reiniciar; volver a
  registrarlas si MediaMTX se reinicia.
- Estado por ruta (vídeo llegando, lectores, bytes, códecs, último error legible).
- Reproducción: /list y URL interna de /get.
- Vigilante de disco cada 60 s (vms.engine.disk_guard).
- Protección contra bloqueos de usuario: si un equipo rechaza la contraseña (401), MediaMTX lo
  reintentaría cada 5 s y Hikvision/Dahua bloquean el usuario tras unos pocos fallos. Tras
  `auth_fail_threshold` rechazos se pausan TODAS las rutas de ese equipo (mismo host, puerto y
  usuario) y solo se vuelve a probar con UNA petición RTSP cada `auth_pause_seconds`, o en cuanto
  cambia la contraseña.
- «Grabando» de verdad: que la ruta tenga vídeo y `record` no basta (el disco puede estar lleno, desconectado
  o sin permiso de escritura). Cada `recording_check_interval` se pide a MediaMTX el último tramo grabado de
  cada ruta que debería grabar, y los errores «[recorder]» de MediaMTX llegan a `PathStatus.recording_error`.
- Retención de cámaras que ya no graban: las carpetas de grabación de cámaras borradas, desactivadas o en
  pausa llevan una ruta inerte (`mtx_config.retention_paths`) para que MediaMTX las siga borrando por
  antigüedad y se puedan reproducir.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlencode, urlsplit

from vms.core.errors import EngineError, EngineUnavailable
from vms.core.interfaces import CameraSource, DiskUsage, EngineProblem, EngineStatus, PathStatus, RecordingSpan
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.mtx_auth import MtxCredentials
from vms.core.naming import mtx_path, parse_mtx_path
from vms.core.paths import AppPaths, find_mediamtx
from vms.core.recordings import (PLAYBACK_DISABLED, PLAYBACK_ENGINE_DOWN, RECORDINGS_DIR_MISSING, record_dir,
                                 recordings_presence)
from vms.core.rtsp import redact
from vms.core.settings import VmsSettings

from . import disk_guard
from . import problem as eproblem
from .logtail import LogTail
from .mtx_api import MediaMtxApi, MtxApiError, format_time, parse_time
from .mtx_config import (attach_config, conf_hash, global_config, http_base, path_configs, path_defaults,
                         render_attach, retention_paths, split_address, write_config, write_if_changed)
from .process import BACKOFF, MediaMtxProcess

log = logging.getLogger("vms.engine")
mtx_log = logging.getLogger("vms.engine.mediamtx")

_LINE_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (DEB|INF|WAR|ERR) (.*)$")
_PATH_RE = re.compile(r"^\[path ([^\]]+)\] (.*)$")
_SOURCE_RE = re.compile(r"^\[[^\]]*source\] (.*)$")
_RECORDER_RE = re.compile(r"^\[recorder\] (.*)$")
_LEVELS = {"DEB": logging.DEBUG, "INF": logging.INFO, "WAR": logging.WARNING, "ERR": logging.ERROR}
# «no recording segments found»: MediaMTX lo registra como ERR cada vez que se consulta un día sin grabaciones.
_QUIET = ("reloading configuration (API request)", "[API] path not found", "[API] path already exists",
          "[playback] no recording segments found")
REPEAT_SILENCE_S = 300.0
DIAG_CACHE_S = 2.0       # status() se pide a menudo (SSE, latido, Estado del sistema cada 5 s)
HOLDER_CACHE_S = 10.0    # psutil.net_connections recorre todas las conexiones del PC
AUTH_PAUSED_MSG = ("Contraseña rechazada por el equipo: se pausó la conexión para que no bloquee el usuario. "
                   "Corrige la contraseña del equipo; mientras tanto se reintenta sola de vez en cuando.")
# «Grabando» de verdad (ver docstring del módulo). MediaMTX escribe una parte cada `part_seconds` (1 s): un tramo
# que no avanza en 45 s es que no se guarda. Recién llegado el vídeo (o recién activada la grabación), el primer
# segmento espera al primer fotograma clave: hasta 90 s no se juzga. Un error «[recorder]» se repite cada ~4 s
# mientras dura (v1.21.1 en Windows: reintenta a los 2 s y falla al escribir): si deja de llegar 30 s, ya pasó.
RECORDING_CHECK_S = 30.0
RECORDING_STALE_S = 45.0
RECORDING_GRACE_S = 90.0
RECORDER_ERROR_TTL_S = 30.0
# Nombre del segmento en los errores «[recorder]» (cambia en cada reintento): fuera de la clave de repetición
_SEGMENT_FILE_RE = re.compile(r"[^\s\\/]+\.(?:mp4|ts)\b", re.IGNORECASE)
RECORDING_STALE_MSG = ("La cámara envía vídeo, pero no se está guardando la grabación. Qué hacer: en «Estado del "
                       "sistema», mira que el «Disco de grabación» esté conectado y tenga espacio; si sigue igual, "
                       "reinicia el PC y, si no se arregla, avisa a soporte.")
_REC_DENIED_RE = re.compile(r"access is denied|acceso denegado|permission denied", re.IGNORECASE)
_REC_FULL_RE = re.compile(r"not enough space|no space left|espacio suficiente|disk full|disco lleno", re.IGNORECASE)
_REC_MISSING_RE = re.compile(r"cannot find the (?:path|file|drive) specified|no such file or directory"
                             r"|device is not ready|no puede encontrar|dispositivo no está listo", re.IGNORECASE)


# Códigos de estado RTSP SOLO cuando el texto los presenta como tales («bad status code: 401
# (Unauthorized)»). Buscar «401» suelto confundía un puerto como 192.168.1.50:5401 con una
# contraseña rechazada y pausaba las cámaras del equipo 30 minutos.
_AUTH_RE = re.compile(r"status code:?\s*401\b|\b401\s*\(unauthorized\)|\bunauthorized\b", re.IGNORECASE)
_NOT_FOUND_RE = re.compile(r"status code:?\s*404\b|\b404\s*\(not found\)|\bnot found\b", re.IGNORECASE)


def is_auth_error(raw: str) -> bool:
    """¿El error de MediaMTX es una contraseña rechazada (RTSP 401)?"""
    return _AUTH_RE.search(raw) is not None


def explain_source_error(raw: str) -> str:
    """Traduce el error de MediaMTX a un texto para el usuario (sin credenciales)."""
    low = raw.lower()
    if is_auth_error(raw):
        msg = "Usuario o contraseña incorrectos en el equipo"
    elif _NOT_FOUND_RE.search(raw):
        msg = "La ruta RTSP no existe en el equipo (revisa el canal o la ruta)"
    elif "connection refused" in low:
        msg = "El equipo rechaza la conexión RTSP (puerto cerrado o servicio RTSP desactivado)"
    elif "timeout" in low or "no route" in low or "unreachable" in low or "deadline" in low:
        msg = "El equipo no responde (sin conexión de red)"
    elif "eof" in low or "reset" in low or "terminated" in low or "closed" in low:
        msg = "El equipo cortó el vídeo; reconectando"
    elif "codec" in low or "unsupported" in low:
        msg = "Formato de vídeo no compatible"
    else:
        msg = "Error en el flujo de vídeo"
    return f"{msg} ({raw.strip()[:160]})"


def explain_recorder_error(raw: str) -> str:
    """Traduce un error «[recorder]» de MediaMTX (no puede escribir el segmento) a un texto con «Qué hacer:».

    Formato real (MediaMTX v1.21.1, Windows): «ERR [path cam-x/main] [recorder] mkdir <carpeta>: The system
    cannot find the path specified.». Se repite cada pocos segundos mientras dure el fallo."""
    if _REC_DENIED_RE.search(raw):
        msg = ("No se puede guardar la grabación: el motor de vídeo no tiene permiso para escribir en la carpeta de "
               "grabaciones. Qué hacer: vuelve a ejecutar el instalador del programa, que repara los permisos de las "
               "carpetas; si sigue igual, avisa a soporte.")
    elif _REC_FULL_RE.search(raw):
        # Los días solo se cambian en el asistente (paso «Días de grabación»), que solo ven los administradores
        msg = ("No se puede guardar la grabación: el disco de grabación está lleno. Qué hacer: libera espacio en ese "
               "disco o guarda menos días de grabación. Para cambiar los días, un administrador abre «Equipos y "
               "monitores», pulsa «Ayuda» > «Primeros pasos (asistente)» y pulsa «Siguiente» hasta el paso «Días de "
               "grabación».")
    elif _REC_MISSING_RE.search(raw):
        msg = ("No se puede guardar la grabación: no se encuentra la carpeta de grabaciones. Qué hacer: si guardas las "
               "grabaciones en un disco externo, conéctalo; si no, mira en «Estado del sistema» qué «Carpeta de "
               "grabaciones» usa el programa y comprueba que existe. Si sigue igual, avisa a soporte.")
    else:
        msg = "No se puede guardar la grabación. Qué hacer: reinicia el PC; si sigue igual, avisa a soporte."
    return f"{msg} (Detalle: {redact(raw.strip())[:160]})"


def segment_starts(folder: Path) -> list[datetime] | None:
    """Inicios (UTC, ordenados) de los segmentos de una carpeta de grabación, por su NOMBRE (sin abrir archivos).
    None si no se puede leer la carpeta.

    Solo los nombres con desfase horario: MediaMTX (`%z` en `RECORD_FILE_PATTERN`) no lista los antiguos. No se usa
    la fecha de modificación: en Windows solo se actualiza al cerrar el archivo."""
    starts: list[datetime] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                m = disk_guard.SEGMENT_RE.match(entry.name)
                start = disk_guard.parse_segment_name(entry.name) if m is not None and m.group(3) else None
                if start is not None:
                    starts.append(start)
    except OSError:
        return None
    return sorted(starts)


def newest_segment_start(folder: Path) -> datetime | None:
    """Inicio (UTC) del segmento más reciente de una carpeta de grabación, por su nombre."""
    starts = segment_starts(folder)
    return starts[-1] if starts else None


@dataclass
class _RecCheck:
    """Última comprobación de que una ruta graba de verdad."""

    at: datetime                    # cuándo se miró (UTC)
    ready_time: str                 # `readyTime` de MediaMTX entonces: si cambia, la cámara se reconectó
    last_end: datetime | None       # final del último tramo grabado (None = nada reciente)
    error: str = ""                 # MediaMTX contestó con un error al pedir el tramo
    since: datetime | None = None   # cuándo se vio que tenía que empezar a grabar («Grabar 24/7» recién marcado):
    #                                 cuenta para el margen como si el vídeo acabara de llegar
    errors: int = 0                 # comprobaciones seguidas con error: uno suelto no basta para «No graba»


def _cred_key(url: str) -> tuple[str, int, str, str]:
    """(host, puerto, usuario, huella de la contraseña). La huella no es reversible y solo vive en memoria."""
    p = urlsplit(url)
    fp = hashlib.sha256(unquote(p.password or "").encode("utf-8")).hexdigest()[:16]
    return (p.hostname or "", p.port or 554, unquote(p.username or ""), fp)


CredKey = tuple[str, int, str, str]


class _AuthGuard:
    """Cuenta rechazos 401 por equipo+credenciales y pausa sus rutas (ver docstring del módulo).

    Al cambiar la contraseña cambia la clave, así que las rutas se reanudan en el acto con la nueva.
    """

    def __init__(self, threshold: int, pause_seconds: float) -> None:
        self.threshold = max(1, threshold)
        self.pause_seconds = pause_seconds
        self.failures: dict[CredKey, int] = {}
        self.paused: dict[CredKey, float] = {}  # clave → monotonic hasta el que dura la pausa

    def failure(self, url: str) -> bool:
        """Registra un rechazo. True si a partir de ahora esas credenciales quedan en pausa."""
        key = _cred_key(url)
        if key in self.paused:
            return False
        self.failures[key] = self.failures.get(key, 0) + 1
        if self.failures[key] >= self.threshold:
            self.paused[key] = time.monotonic() + self.pause_seconds
            return True
        return False

    def success(self, url: str) -> None:
        self.failures.pop(_cred_key(url), None)

    def is_paused(self, url: str) -> bool:
        return _cred_key(url) in self.paused

    def due(self) -> list[CredKey]:
        now = time.monotonic()
        return [k for k, until in self.paused.items() if until <= now]

    def extend(self, key: CredKey) -> None:
        self.paused[key] = time.monotonic() + self.pause_seconds

    def release(self, key: CredKey) -> None:
        self.paused.pop(key, None)
        self.failures.pop(key, None)

    def forget_unused(self, used: set[CredKey]) -> None:
        for key in [k for k in self.paused if k not in used]:
            self.release(key)


EngineMode = Literal["child", "attach"]
EventSink = Callable[[dict[str, Any]], None]


class MediaMtxEngine:
    def __init__(self, settings: VmsSettings, paths: AppPaths, *, mediamtx_bin: Path | None = None,
                 disk_guard_interval: float = 60.0, watchdog_interval: float = 15.0,
                 auth_fail_threshold: int = 2, auth_pause_seconds: float = 1800.0,
                 backoff: tuple[float, ...] = BACKOFF, api_timeout: float = 5.0,
                 mode: EngineMode | None = None, attach_poll: float = 2.0,
                 logtail_interval: float = 1.0, recording_check_interval: float = RECORDING_CHECK_S) -> None:
        self.settings = settings
        self.paths = paths
        self.mode: EngineMode = mode or settings.engine_mode
        self.attach_poll = attach_poll
        self.logtail_interval = logtail_interval
        # Quien quiera el evento SSE `engine` (CONTRATO §17.3) asigna aquí `lambda ev: publish_engine(bus, ev)`.
        self.on_event: EventSink | None = None
        self._api_ok = False
        self._api_status: int | None = None   # código HTTP con el que falló la API (401: clave interna rechazada)
        self._mtx_started = ""
        self._attach_restarts = 0
        # Por qué no funciona el motor (modo attach): lo que dice engine.log y status-VMSEngine.json
        self._cause = eproblem.EngineLogCause()
        self._port_roles = eproblem.port_roles(settings)
        self._diag_lock = asyncio.Lock()
        self._diag: tuple[float, tuple[bool, int | None], eproblem.RunStatus | None, EngineProblem | None] | None = None
        self._holders: dict[tuple[int, str, str], tuple[float, eproblem.PortHolder | None]] = {}
        self._problem_logged = ""
        self._run_status_warned = False
        self._diag_failed = False
        self.logtail: LogTail | None = None
        self.exe = mediamtx_bin or settings.mediamtx_bin or find_mediamtx()
        api_hp = split_address(settings.mtx_api_address)
        if api_hp is None:
            raise ValueError("VMS_MTX_API_ADDRESS no puede estar vacío")
        self._api_host, self._api_port = api_hp
        self._api_base = http_base(settings.mtx_api_address) or ""
        self._playback_base = http_base(settings.mtx_playback_address)
        self._webrtc_base = http_base(settings.mtx_webrtc_address)
        self._api_timeout = api_timeout
        self.mtx_credentials = MtxCredentials.from_internal_token(settings.ensure_internal_token())
        self.disk_guard_interval = disk_guard_interval
        self.watchdog_interval = watchdog_interval
        self.recording_check_interval = recording_check_interval
        self.backoff = backoff
        self.config_file = paths.mediamtx_dir / "mediamtx.yml"

        self._recording = RecordingSettings()
        self._retention = RetentionSettings()
        self._recordings_dir = str(paths.recordings_dir)
        self._sources: dict[str, CameraSource] = {}
        self._applied: dict[str, str] = {}          # ruta → huella de la conf registrada en MediaMTX
        self._path_errors: dict[str, str] = {}
        self._recorder_errors: dict[str, tuple[datetime, str]] = {}   # ruta → (cuándo, texto para el usuario)
        self._rec_checks: dict[str, _RecCheck] = {}
        self._rec_wanted: set[str] | None = None    # rutas con `record` en la comprobación anterior (None = aún no hubo)
        self._log_seen: dict[tuple[str, str], float] = {}      # errores de rutas ya registrados
        self._engine_seen: dict[tuple[str, str], float] = {}   # y del propio MediaMTX (aparte: no vacían los otros)
        self._auth = _AuthGuard(auth_fail_threshold, auth_pause_seconds)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._version = ""
        self._disk_warning = ""
        self.disk_guard_last: disk_guard.GuardResult | None = None
        self.process: MediaMtxProcess | None = None
        self.api: MediaMtxApi | None = None

    # ================================================================== ciclo de vida
    async def start(self) -> None:
        if self.mode == "attach":
            await self._start_attach()
            return
        if self.process is not None and self.process.running:
            return
        if self.exe is None or not Path(self.exe).is_file():
            raise EngineUnavailable("No se encuentra el ejecutable de MediaMTX. Instálalo en <instalación>/bin "
                                    "o indica su ruta en VMS_MEDIAMTX_BIN.")
        self.paths.ensure()
        Path(self._recordings_dir).mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(disk_guard.migrate_legacy_names, Path(self._recordings_dir))
        except Exception:  # noqa: BLE001 - no impide arrancar; solo afecta a grabaciones antiguas
            log.exception("Error renombrando grabaciones antiguas al formato con desfase horario")
        self.api = MediaMtxApi(self._api_base, self._playback_base, timeout=self._api_timeout,
                               auth=self.mtx_credentials.api_auth)
        self.process = MediaMtxProcess(Path(self.exe), self.config_file, workdir=self.paths.mediamtx_dir,
                                       api_host=self._api_host, api_port=self._api_port, on_line=self._on_line,
                                       on_restart=self._on_restart, before_spawn=self._write_yaml,
                                       backoff=self.backoff, api_auth=self.mtx_credentials.api_auth)
        try:
            await self.process.start()
        except EngineUnavailable:
            await self.api.aclose()
            self.api = None
            raise
        await self._refresh_version()
        async with self._lock:
            self._applied.clear()
            await self._sync_locked()
        self._tasks = [asyncio.create_task(self._disk_guard_loop(), name="engine-disk-guard"),
                       asyncio.create_task(self._watchdog_loop(), name="engine-watchdog"),
                       asyncio.create_task(self._recording_check_loop(), name="engine-recording-check")]
        log.info("Motor de vídeo en marcha (MediaMTX %s)", self._version or "?")
        self._emit("started", self.process.pid)

    async def _start_attach(self) -> None:
        """Modo attach: no lanza nada. Prepara la API, sigue `engine.log` y vigila al servicio VMSEngine.

        No escribe el YAML aquí: lo escribe `apply()` con las cámaras ya cargadas. Escribirlo vacío al
        arrancar quitaría todas las rutas un instante y abriría un hueco en las grabaciones."""
        if self._tasks:
            return
        self.paths.ensure()
        Path(self._recordings_dir).mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(disk_guard.migrate_legacy_names, Path(self._recordings_dir))
        except Exception:  # noqa: BLE001 - no impide arrancar; solo afecta a grabaciones antiguas
            log.exception("Error renombrando grabaciones antiguas al formato con desfase horario")
        self.api = MediaMtxApi(self._api_base, self._playback_base, timeout=self._api_timeout,
                               auth=self.mtx_credentials.api_auth)
        await self._probe_engine()
        self.logtail = LogTail(self.paths.logs_dir / "engine.log", lambda line: self._on_line(line, relay=False))
        if not self._api_ok:
            # LogTail empieza por el final: el motivo de una caída de antes de arrancar el backend (servicio de
            # inicio retrasado) está en lo último de engine.log
            try:
                await asyncio.to_thread(self._scan_engine_log)
            except Exception:  # noqa: BLE001 - solo sirve para explicar la caída: no puede impedir arrancar
                log.exception("Error leyendo el final de engine.log")
        self._tasks = [asyncio.create_task(self._disk_guard_loop(), name="engine-disk-guard"),
                       asyncio.create_task(self._attach_watchdog(), name="engine-attach-watchdog"),
                       asyncio.create_task(self.logtail.run(self.logtail_interval), name="engine-logtail"),
                       asyncio.create_task(self._recording_check_loop(), name="engine-recording-check")]
        if self._api_ok:
            log.info("Motor de vídeo (servicio VMSEngine, MediaMTX %s) conectado en modo attach", self._version or "?")
        else:
            log.warning("El motor de vídeo (servicio VMSEngine) todavía no responde: el backend sigue y se conectará "
                        "en cuanto aparezca. Las cámaras se guardan igualmente en mediamtx.yml")

    def _emit(self, state: Literal["started", "restarted", "down"], pid: int | None = None) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({"state": state, "pid": pid,
                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")})
        except Exception:  # noqa: BLE001 - un oyente roto no puede parar el motor
            log.exception("Error avisando del estado del motor")

    async def _probe_engine(self) -> None:
        """Modo attach: ¿responde el servicio VMSEngine? Detecta caídas y reinicios (cambia `started`)."""
        if self.api is None:
            return
        was_ok = self._api_ok
        try:
            info = await self.api.info()
        except (EngineUnavailable, MtxApiError) as exc:
            self._api_ok = False
            self._api_status = exc.status if isinstance(exc, MtxApiError) else None
            if was_ok:
                log.warning("El motor de vídeo (VMSEngine) dejó de responder: %s", exc)
                self._emit("down")
            return
        self._api_ok = True
        self._api_status = None
        self._version = str(info.get("version", "")) or self._version
        started = str(info.get("started", ""))
        if not self._mtx_started:
            event: Literal["started", "restarted"] | None = "started"
        elif started and started != self._mtx_started:
            self._attach_restarts += 1
            self._path_errors.clear()
            log.warning("El motor de vídeo (VMSEngine) se reinició; vuelve a grabar solo con mediamtx.yml")
            event = "restarted"
        elif not was_ok:
            event = "started"
        else:
            event = None
        self._mtx_started = started or self._mtx_started or "?"
        if event is not None:
            if not was_ok and event == "started" and self._tasks:
                log.info("El motor de vídeo (VMSEngine) vuelve a responder")
            self._emit(event)

    async def _attach_watchdog(self) -> None:
        since_retry = 0.0
        while True:
            await asyncio.sleep(self.attach_poll)
            try:
                await self._probe_engine()
                since_retry += self.attach_poll
                if since_retry >= self.watchdog_interval:
                    since_retry = 0.0
                    released = await self._retry_paused()
                    if released:
                        await self._sync_soon()
            except Exception:  # noqa: BLE001 - la vigilancia no puede morir
                log.exception("Error en la vigilancia del motor (modo attach)")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("Error al detener una tarea del motor")
        self._tasks = []
        if self.process is not None:
            await self.process.stop()
        if self.api is not None:
            await self.api.aclose()
            self.api = None
        self._applied.clear()

    @property
    def running(self) -> bool:
        if self.mode == "attach":
            return self._api_ok
        return self.process is not None and self.process.running

    def _write_yaml(self) -> None:
        """Genera mediamtx.yml. Solo con MediaMTX parado (lo llama el supervisor antes de lanzarlo)."""
        write_config(self.config_file, global_config(self.settings, self._recording, self._retention,
                                                     self._recordings_dir, creds=self.mtx_credentials))

    async def _refresh_version(self) -> None:
        if self.api is None:
            return
        try:
            self._version = str((await self.api.info()).get("version", ""))
        except (EngineUnavailable, MtxApiError) as exc:
            log.debug("No se pudo leer la versión de MediaMTX: %s", exc)

    async def _on_restart(self) -> None:
        log.warning("MediaMTX se reinició: se vuelven a registrar las rutas de las cámaras")
        self._path_errors.clear()
        await self._refresh_version()
        async with self._lock:
            self._applied.clear()
            await self._sync_locked()
        self._emit("restarted", self.process.pid if self.process else None)

    # ================================================================== configuración
    async def apply(self, sources: list[CameraSource], recording: RecordingSettings,
                    retention: RetentionSettings, recordings_dir: str) -> None:
        async with self._lock:
            defaults_changed = (recording != self._recording or retention != self._retention
                                or str(recordings_dir) != self._recordings_dir)
            self._recording = recording.model_copy()
            self._retention = retention.model_copy()
            self._recordings_dir = str(recordings_dir)
            self._sources = {s.camera_id: s for s in sources}
            # Credenciales que ya no usa ninguna cámara (p. ej. contraseña corregida): fuera de la pausa
            self._auth.forget_unused({_cred_key(s.main_url) for s in sources})
            if defaults_changed:
                # Modo child: el YAML NO se reescribe aquí (MediaMTX recargaría el archivo y perdería las
                # rutas registradas por la API); se regenera antes de cada arranque y en caliente se cambia
                # por la API. Modo attach: el YAML lo es todo y se reescribe abajo.
                Path(self._recordings_dir).mkdir(parents=True, exist_ok=True)
            if self.mode == "attach":
                if defaults_changed and self.config_file.exists():
                    log.info("Grabación: segmentos de %ss, retención %s días, carpeta %s. Cambiar estos ajustes "
                             "abre un segmento nuevo en todas las cámaras (hueco de hasta 1 GOP)",
                             recording.segment_seconds, retention.days, self._recordings_dir)
                await self._write_attach_yaml()
                return
            if not self.running or self.api is None:
                raise EngineUnavailable("El motor de vídeo no está en marcha; se aplicará al arrancar")
            if defaults_changed:
                try:
                    await self.api.patch_path_defaults(path_defaults(recording, retention, self._recordings_dir))
                except MtxApiError as exc:
                    raise EngineUnavailable(f"MediaMTX rechazó la configuración de grabación: {exc.message}") from exc
                log.info("Grabación: segmentos de %ss, retención %s días, carpeta %s", recording.segment_seconds,
                         retention.days, self._recordings_dir)
            await self._sync_locked()

    def _desired(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for src in self._sources.values():
            out.update(path_configs(src))
        return out

    async def _write_attach_yaml(self) -> bool:
        """Modo attach: escribe mediamtx.yml completo si cambió (requiere self._lock). True si lo escribió.

        Las rutas de un equipo en pausa por contraseña rechazada se quitan del YAML (CONTRATO §13.10); si tienen
        grabaciones, quedan como ruta inerte (solo retención y reproducción, ver `retention_paths`)."""
        desired = self._desired()
        active: dict[str, dict[str, Any]] = {}
        for name, conf in desired.items():
            if self._auth.is_paused(conf["source"]):
                self._path_errors[name] = AUTH_PAUSED_MSG
            else:
                active[name] = conf
        for name in list(self._path_errors):
            if name not in desired:
                self._path_errors.pop(name, None)
        inert = await asyncio.to_thread(retention_paths, self._recordings_dir, active)
        cfg = attach_config(self.settings, self._recording, self._retention, self._recordings_dir,
                            {**inert, **active}, creds=self.mtx_credentials)
        changed = await asyncio.to_thread(write_if_changed, self.config_file, render_attach(cfg))
        self._applied = {name: conf_hash(conf) for name, conf in active.items()}
        if changed:
            log.info("mediamtx.yml actualizado (%d rutas y %d solo para grabaciones antiguas): MediaMTX lo recarga "
                     "solo", len(active), len(inert))
        return changed

    async def _sync_locked(self) -> None:
        """Alinea las rutas de MediaMTX con las cámaras deseadas (requiere self._lock)."""
        if self.mode == "attach":
            await self._write_attach_yaml()
            return
        if self.api is None or not self.running:
            raise EngineUnavailable("El motor de vídeo no está en marcha")
        desired = self._desired()
        active = {n: c for n, c in desired.items() if not self._auth.is_paused(c["source"])}
        # Cámaras sin ruta activa pero con grabaciones: ruta inerte (retención y reproducción, ver retention_paths)
        wanted = {**await asyncio.to_thread(retention_paths, self._recordings_dir, active), **active}
        present = await self.api.config_path_names()
        for name in sorted(present):
            if parse_mtx_path(name) is None:
                continue
            if name not in wanted:
                if await self.api.delete_path(name):
                    log.info("Ruta %s retirada del motor", name)
                self._applied.pop(name, None)
        present = {n for n in present if n in wanted}
        for name in desired:
            if name not in active:
                self._path_errors[name] = AUTH_PAUSED_MSG
        for name, conf in wanted.items():
            h = conf_hash(conf)
            try:
                if name not in present:
                    await self.api.add_path(name, conf)
                    log.info("Ruta %s registrada en el motor", name)
                elif self._applied.get(name) != h:
                    await self.api.replace_path(name, conf)
                    log.info("Ruta %s actualizada", name)
                self._applied[name] = h
            except MtxApiError as exc:
                self._applied.pop(name, None)
                self._path_errors[name] = f"MediaMTX rechazó la configuración de la ruta: {exc.message}"
                log.error("MediaMTX rechazó la ruta %s: %s", name, exc.message)
        for name in list(self._path_errors):
            if name not in desired:
                self._path_errors.pop(name, None)

    async def _sync_soon(self) -> None:
        try:
            async with self._lock:
                await self._sync_locked()
        except EngineUnavailable as exc:
            log.debug("Sincronización aplazada: %s", exc.message)
        except Exception:  # noqa: BLE001
            log.exception("Error sincronizando las rutas del motor")

    # ================================================================== salida de MediaMTX
    def _on_line(self, line: str, relay: bool = True) -> None:
        """Interpreta una línea de MediaMTX. `relay=False` (modo attach, la línea ya está en engine.log): solo
        se repiten en el registro del backend los avisos y errores, no todo."""
        if self.mode == "attach":
            self._cause.feed(line)   # eventos de vmsctl, «ERR: …» y errores generales: por qué no arranca
        m = _LINE_RE.match(line)
        if not m:
            if relay:
                mtx_log.info("%s", line)
            return
        level, msg = _LEVELS[m.group(1)], m.group(2)
        pm = _PATH_RE.match(msg)
        if pm:
            name, rest = pm.group(1), pm.group(2)
            sm = _SOURCE_RE.match(rest)
            rm = _RECORDER_RE.match(rest)
            if sm and m.group(1) in ("ERR", "WAR"):
                raw = sm.group(1)
                self._path_errors[name] = explain_source_error(raw)
                if is_auth_error(raw) and name in self._applied:
                    self._on_auth_failure(name)
                if self._repeated((name, raw)):
                    level = logging.DEBUG  # el mismo error cada 5 s no debe inundar el registro
            elif rm and m.group(1) == "ERR":
                # El grabador no puede escribir (permisos, disco lleno o desconectado): la ruta tiene vídeo, pero
                # NO graba. Se borra cuando la comprobación periódica ve un tramo nuevo (check_recording).
                raw = rm.group(1)
                self._recorder_errors[name] = (datetime.now(timezone.utc), explain_recorder_error(raw))
                # «open …\<segmento>.mp4: Access is denied.» cambia de archivo en cada reintento (~4 s)
                if self._repeated((name, _SEGMENT_FILE_RE.sub("<segmento>", raw))):
                    level = logging.DEBUG
            elif "is available and online" in rest or "is ready" in rest:
                if name in self._path_errors and self._path_errors[name] != AUTH_PAUSED_MSG:
                    self._path_errors.pop(name, None)
                parsed = parse_mtx_path(name)
                src = self._sources.get(parsed[0]) if parsed else None
                if src is not None:
                    self._auth.success(src.main_url)
        elif m.group(1) in ("ERR", "WAR") and self._repeated(("", msg)):
            # Errores del propio MediaMTX: el mismo «listen tcp …: bind» llegaba a vms.log en cada relanzamiento
            # (cada 30 s; 1568 veces en el PC del usuario). El motivo sigue en Estado del sistema.
            level = logging.DEBUG
        if any(q in msg for q in _QUIET):
            level = logging.DEBUG
        if relay or level >= logging.WARNING:
            mtx_log.log(level, "%s", msg)

    def _repeated(self, key: tuple[str, str]) -> bool:
        """¿Ya se registró este mismo error hace menos de REPEAT_SILENCE_S? (entonces va a DEBUG). `key[0]` es la
        ruta; "" para los del propio MediaMTX, que pueden llevar identificadores de conexión y van aparte."""
        seen = self._log_seen if key[0] else self._engine_seen
        now = time.monotonic()
        if now - seen.get(key, -1e9) < REPEAT_SILENCE_S:
            return True
        seen[key] = now
        if len(seen) > 2000:
            seen.clear()
        return False

    def _on_auth_failure(self, name: str) -> None:
        parsed = parse_mtx_path(name)
        src = self._sources.get(parsed[0]) if parsed else None
        if src is None:
            return
        url = src.main_url if parsed and parsed[1] == "main" else (src.sub_url or src.main_url)
        if self._auth.failure(url):
            host, port, user, _ = _cred_key(url)
            log.error("El equipo %s:%s rechaza la contraseña del usuario «%s». Se pausan sus cámaras para no "
                      "bloquear el usuario; se volverá a probar en %.0f min o al cambiar la contraseña.",
                      host, port, user, self._auth.pause_seconds / 60)
            asyncio.get_running_loop().create_task(self._sync_soon())

    # ================================================================== tareas de fondo
    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.watchdog_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._retry_paused()
                if self.running:
                    async with self._lock:
                        await self._sync_locked()
            except EngineUnavailable as exc:
                log.debug("Vigilancia del motor: %s", exc.message)
            except Exception:  # noqa: BLE001 - la vigilancia no puede morir
                log.exception("Error en la vigilancia del motor")

    async def _retry_paused(self) -> bool:
        """Vuelve a probar (con UNA petición) los equipos en pausa. True si alguno se reanudó."""
        from vms.vendors.rtsp_probe import probe_rtsp

        released = False
        for key in self._auth.due():
            src = next((s for s in self._sources.values() if _cred_key(s.main_url) == key), None)
            if src is None:
                self._auth.release(key)
                continue
            p = urlsplit(src.main_url)
            path = p.path + (f"?{p.query}" if p.query else "")
            result = await probe_rtsp(p.hostname or "", p.port or 554, path, unquote(p.username or ""),
                                      unquote(p.password or ""), timeout=5.0)
            if result.status == 401:
                self._auth.extend(key)
                log.warning("El equipo %s:%s sigue rechazando la contraseña; cámaras en pausa otros %.0f min",
                            key[0], key[1], self._auth.pause_seconds / 60)
            else:
                log.info("El equipo %s:%s vuelve a aceptar la contraseña; se reanudan sus cámaras", key[0], key[1])
                self._auth.release(key)
                released = True
                for name, msg in list(self._path_errors.items()):
                    if msg == AUTH_PAUSED_MSG:
                        self._path_errors.pop(name, None)
        return released

    async def _recording_check_loop(self) -> None:
        while True:
            await asyncio.sleep(self.recording_check_interval)
            try:
                await self.check_recording()
            except (EngineUnavailable, MtxApiError) as exc:
                log.debug("Comprobación de la grabación aplazada: %s", exc)
            except Exception:  # noqa: BLE001 - la vigilancia no puede morir
                log.exception("Error comprobando que las cámaras graban")

    async def check_recording(self) -> None:
        """¿Graban de verdad las rutas que dicen grabar? Pide a MediaMTX el último tramo de cada una.

        Solo se pregunta por el segmento más reciente de la carpeta (por su nombre), así /list lee una sola parte
        del disco y nunca contesta 404/400: MediaMTX registraría un ERR en engine.log en cada comprobación. Si el
        último segmento empezó hace más de un segmento y dos minutos, ni se pregunta: no se está grabando.

        Sin servidor de reproducción (desactivado o caído) no se puede comprobar: esas rutas se quedan con la palabra
        de MediaMTX y con los errores «[recorder]» que sigan llegando."""
        if self.api is None or not self.running:
            return
        items = {str(i.get("name", "")): i for i in await self.api.paths_list()}
        now = datetime.now(timezone.utc)
        desired = self._desired()
        checked: set[str] = set()
        can_list = True
        for name, conf in desired.items():
            parsed = parse_mtx_path(name)
            item = items.get(name) or {}
            if (parsed is None or parsed[1] != "main" or not conf.get("record") or not item.get("ready")
                    or self._auth.is_paused(conf["source"])):
                continue
            newest = await asyncio.to_thread(newest_segment_start, record_dir(self._recordings_dir, parsed[0]))
            last_end: datetime | None = None
            error = ""
            if newest is not None and (now - newest).total_seconds() <= self._recording.segment_seconds + 120:
                if not can_list:
                    continue
                try:
                    spans = await self.api.list_recordings(name, newest - timedelta(seconds=1), None)
                except EngineUnavailable as exc:
                    log.debug("No se puede comprobar si graba %s: %s", name, exc.message)
                    can_list = False   # el mismo motivo valdría para las demás: ni se pregunta
                    continue
                except EngineError as exc:
                    error = exc.message
                else:
                    last_end = max((s.end for s in spans), default=None)
            checked.add(name)
            ready_time = str(item.get("readyTime") or "")
            prev = self._rec_checks.get(name)
            same = prev is not None and prev.ready_time == ready_time
            if self._rec_wanted is not None and name not in self._rec_wanted:
                since: datetime | None = now   # «Grabar 24/7» recién marcado: MediaMTX no reinicia la ruta
            else:
                since = prev.since if same and prev is not None else None
            errors = ((prev.errors if same and prev is not None else 0) + 1) if error else 0
            self._rec_checks[name] = _RecCheck(at=now, ready_time=ready_time, last_end=last_end, error=error,
                                               since=since, errors=errors)
            pending = self._recorder_errors.get(name)
            if (pending is not None and last_end is not None and last_end >= pending[0]
                    and (now - last_end).total_seconds() <= RECORDING_STALE_S):
                self._recorder_errors.pop(name, None)   # ha vuelto a guardar después del error
        for name in [n for n in self._rec_checks if n not in checked]:
            self._rec_checks.pop(name, None)
        for name, (at, _) in list(self._recorder_errors.items()):
            if name not in desired or (now - at).total_seconds() > RECORDER_ERROR_TTL_S:
                self._recorder_errors.pop(name, None)
        self._rec_wanted = {n for n, c in desired.items() if c.get("record")}

    def _recording_verdict(self, name: str, item: dict[str, Any]) -> tuple[bool, str]:
        """(¿graba?, motivo si no) de una ruta principal con vídeo y `record`."""
        ready_time = str(item.get("readyTime") or "")
        try:
            ready_since = parse_time(ready_time) if ready_time else None
        except ValueError:
            ready_since = None
        pending = self._recorder_errors.get(name)
        # Un error del grabador vale mientras se sigue repitiendo y si es de esta conexión, no de antes de reconectarse
        if (pending is not None
                and (datetime.now(timezone.utc) - pending[0]).total_seconds() <= RECORDER_ERROR_TTL_S
                and (ready_since is None or pending[0] >= ready_since)):
            return False, pending[1]
        chk = self._rec_checks.get(name)
        if chk is None or chk.ready_time != ready_time:
            return True, ""   # aún sin comprobar desde que llegó el vídeo: se da por buena la palabra de MediaMTX
        if chk.last_end is not None and (chk.at - chk.last_end).total_seconds() <= RECORDING_STALE_S:
            return True, ""
        start = max((t for t in (ready_since, chk.since) if t is not None), default=None)
        if start is not None and (chk.at - start).total_seconds() < RECORDING_GRACE_S:
            return True, ""   # recién conectada o recién activada: el primer segmento espera al fotograma clave
        if chk.error and chk.errors < 2:
            return True, ""   # un error suelto de /list no basta: se espera a la siguiente comprobación
        return False, chk.error or RECORDING_STALE_MSG

    async def run_disk_guard(self) -> disk_guard.GuardResult:
        percent = float(self._retention.disk_guard_percent)
        res = await asyncio.to_thread(disk_guard.run, Path(self._recordings_dir), percent)
        self.disk_guard_last = res
        if percent and not res.target_reached:
            self._disk_warning = (f"Disco de grabación al {res.percent_after:.0f} % (umbral {percent:.0f} %) "
                                  "y no se puede liberar más borrando grabaciones antiguas")
        else:
            self._disk_warning = ""
        return res

    async def _disk_guard_loop(self) -> None:
        while True:
            try:
                await self.run_disk_guard()
            except Exception:  # noqa: BLE001 - la vigilancia no puede morir
                log.exception("Error en el vigilante de disco")
            await asyncio.sleep(self.disk_guard_interval)

    # ================================================================== consultas
    async def status(self) -> EngineStatus:
        if self.mode == "attach":
            await self._probe_engine()
            try:
                started_at = parse_time(self._mtx_started) if self._api_ok and self._mtx_started != "?" else None
            except ValueError:
                started_at = None
            run, prob = await self._diagnose()
            now = time.time()
            return EngineStatus(running=self._api_ok, pid=None, version=self._version,
                                restarts=self._attach_restarts, started_at=started_at, api_ok=self._api_ok,
                                last_error=eproblem.as_text(prob) if prob else self._disk_warning, problem=prob,
                                service_state=run.state if run else "",
                                crashes_10min=run.crashes(now) if run else self._cause.snapshot().crashes(now))
        proc = self.process
        api_ok = False
        if self.api is not None and self.running:
            try:
                await self.api.info()
                api_ok = True
            except (EngineUnavailable, MtxApiError):
                api_ok = False
        last_error = ""
        if proc is not None and proc.last_error and (not self.running or not api_ok):
            last_error = proc.last_error
        elif self._disk_warning:
            last_error = self._disk_warning
        return EngineStatus(running=self.running, pid=proc.pid if proc else None, version=self._version,
                            restarts=proc.restarts if proc else 0,
                            started_at=proc.started_at if proc and self.running else None,
                            api_ok=api_ok, last_error=last_error)

    # ------------------------------------------------------------------ por qué no funciona (modo attach)
    def _scan_engine_log(self) -> None:
        """Final de engine.log (64 KB) al arrancar con el motor caído. Solo para explicar la caída: nunca pausa
        equipos por 401 antiguos. Antes, LogTail apunta dónde empieza a seguir: lo que vmsctl escriba mientras
        tanto lo lee LogTail, sin perderlo ni repetirlo."""
        end = None
        if self.logtail is not None:
            self.logtail.read_new()   # primera vuelta: solo fija el final de ahora (empieza por el final)
            end = self.logtail.offset
        for line in eproblem.read_log_tail(self.paths.logs_dir / "engine.log", end=end):
            self._cause.feed(line)

    async def _diagnose(self) -> tuple[eproblem.RunStatus | None, EngineProblem | None]:
        """Estado de `vmsctl run` y, si la API no responde, el problema (caché de DIAG_CACHE_S)."""
        key = (self._api_ok, self._api_status)
        async with self._diag_lock:
            if self._diag is not None and self._diag[1] == key and time.monotonic() - self._diag[0] < DIAG_CACHE_S:
                return self._diag[2], self._diag[3]
            cause = self._cause.snapshot()   # foto: el bucle de eventos sigue alimentándolo
            try:
                run, prob = await asyncio.to_thread(self._diagnose_sync, key, cause)
            except Exception:  # noqa: BLE001 - explicar la caída nunca puede tumbar el estado del motor
                if not self._diag_failed:
                    log.exception("Error averiguando por qué no responde el motor de vídeo")
                self._diag_failed = True
                run, prob = None, None
            else:
                self._diag_failed = False
            self._diag = (time.monotonic(), key, run, prob)
        self._log_problem(prob)
        return run, prob

    def _diagnose_sync(self, key: tuple[bool, int | None], cause: eproblem.LogCause
                       ) -> tuple[eproblem.RunStatus | None, EngineProblem | None]:
        run, why = eproblem.read_run_status(self.paths.logs_dir / "status-VMSEngine.json")
        if why == "denied" and not self._run_status_warned:
            self._run_status_warned = True
            log.warning("No se puede leer logs/status-VMSEngine.json (permiso denegado): Estado del sistema dirá "
                        "menos sobre el motor. Qué hacer: vuelve a ejecutar el instalador para reparar los permisos")
        prob = eproblem.diagnose(api_ok=key[0], api_status=key[1], run=run, cause=cause, now=time.time(),
                                 roles=self._port_roles, holder=self._port_holder)
        return run, prob

    def _port_holder(self, port: int, proto: str, host: str) -> eproblem.PortHolder | None:
        hit = self._holders.get((port, proto, host))
        if hit is not None and time.monotonic() - hit[0] < HOLDER_CACHE_S:
            return hit[1]
        who = eproblem.port_holder(port, proto, host)
        self._holders[(port, proto, host)] = (time.monotonic(), who)
        return who

    def _log_problem(self, prob: EngineProblem | None) -> None:
        """Una línea en vms.log cuando cambia el motivo (está en el paquete de diagnóstico), no en cada consulta."""
        key = f"{prob.code}|{prob.action_es}" if prob is not None and prob.code != "starting" else ""
        if key and key != self._problem_logged and prob is not None:
            log.warning("Motor de vídeo: %s", eproblem.as_text(prob))
        self._problem_logged = key

    async def paths_status(self) -> dict[str, PathStatus]:
        if self.api is None or not self.running:
            raise EngineUnavailable("El motor de vídeo no está en marcha")
        desired = self._desired()
        out: dict[str, PathStatus] = {}
        for item in await self.api.paths_list():
            name = str(item.get("name", ""))
            parsed = parse_mtx_path(name)
            if parsed is None:
                continue
            ready = bool(item.get("ready"))
            conf = desired.get(name) or {}
            recording, rec_error = False, ""
            if ready and conf.get("record"):
                recording, rec_error = self._recording_verdict(name, item)
            chk = self._rec_checks.get(name)
            out[name] = PathStatus(
                name=name, camera_id=parsed[0], stream=parsed[1],  # type: ignore[arg-type]
                ready=ready, source_online=ready, readers=len(item.get("readers") or []),
                bytes_received=int(item.get("inboundBytes") or item.get("bytesReceived") or 0),
                tracks=[str(t) for t in (item.get("tracks") or [])],
                recording=recording, recording_error=rec_error,
                last_recorded_at=chk.last_end if chk is not None else None,
                last_error=rec_error if ready else self._path_errors.get(name, ""))
        for name in desired:
            if name not in out:
                parsed = parse_mtx_path(name)
                assert parsed is not None
                out[name] = PathStatus(name=name, camera_id=parsed[0], stream=parsed[1],  # type: ignore[arg-type]
                                       last_error=self._path_errors.get(name, ""))
        return out

    async def list_recordings(self, camera_id: str, start: datetime | None,
                              end: datetime | None) -> list[RecordingSpan]:
        """Tramos grabados (CONTRATO §6.7). Antes de preguntar a MediaMTX se mira la carpeta de la cámara:

        - sin carpeta de grabaciones general → EngineError (disco desconectado; no es «nunca ha grabado»);
        - sin carpeta de la cámara → [] sin preguntar (MediaMTX contestaría 400 y dejaría un ERR en engine.log cada
          vez; la vigilancia de operación pregunta cada 10 min por cámara). Si la cámara está en el motor y este no
          responde, se dice eso;
        - con carpeta, si por los nombres de los segmentos no puede haber nada en el rango (carpeta vacía, rango
          anterior al primer segmento o posterior al final del último que empieza antes) → [] sin preguntar: MediaMTX
          contestaría 404 y dejaría otro ERR (p. ej. el resumen de hoy de una cámara apagada todo el día);
        - una cámara desactivada o borrada solo tiene ruta en MediaMTX si tiene grabaciones (`retention_paths`):
          si aún no ha llegado al motor, [] en vez de «no configurada»."""
        if self.api is None:
            raise EngineUnavailable(PLAYBACK_ENGINE_DOWN)
        presence = await asyncio.to_thread(recordings_presence, self._recordings_dir, camera_id)
        if presence == "no_root":
            raise EngineError(RECORDINGS_DIR_MISSING, details={"reason": "no_root"})
        path = mtx_path(camera_id, "main")
        try:
            if presence == "none" or (presence == "present" and not await self._may_have_recordings(
                    path, record_dir(self._recordings_dir, camera_id), start, end)):
                if camera_id in self._sources and not self.running:
                    raise EngineUnavailable(PLAYBACK_ENGINE_DOWN)
                return []
            return await self.api.list_recordings(path, start, end)
        except EngineError as exc:
            if exc.details.get("kind") == "not_configured" and camera_id not in self._sources:
                return []
            raise

    async def _may_have_recordings(self, path: str, folder: Path, start: datetime | None,
                                   end: datetime | None) -> bool:
        """¿Puede haber grabación de la ruta en [start, end)? Por los nombres de los segmentos y, si hace falta,
        preguntando a MediaMTX solo por el último segmento que empieza antes del rango (eso nunca da 404).

        Un segmento empieza a la hora de su nombre, pero cuánto dura no se sabe sin leerlo: puede seguir abierto
        (en Windows la fecha de modificación no avanza hasta cerrarlo) o venir de cuando los segmentos eran más
        largos. Si alguno empieza dentro del rango, seguro que hay algo."""
        if start is not None and start.tzinfo is None:   # sin zona = UTC, como en `mtx_api.format_time`
            start = start.replace(tzinfo=timezone.utc)
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        starts = await asyncio.to_thread(segment_starts, folder)
        if starts is None:
            return True   # no se pudo leer la carpeta: que conteste MediaMTX, que dará el error real
        if not starts or (end is not None and end <= starts[0]):
            return False
        if start is None or any(s >= start and (end is None or s < end) for s in starts):
            return True
        before = [s for s in starts if s < start]
        if not before:
            return False   # todos empiezan después del rango
        if self.api is None:
            raise EngineUnavailable(PLAYBACK_ENGINE_DOWN)
        # [inicio del segmento − 1 s, start + 1 s]: si el segmento llega a `start`, el tramo que devuelve MediaMTX
        # (recortado al final pedido) acaba después de `start`
        probe_end = start + timedelta(seconds=1) if end is None else min(start + timedelta(seconds=1), end)
        spans = await self.api.list_recordings(path, before[-1] - timedelta(seconds=1), probe_end)
        return any(sp.end > start for sp in spans)

    def playback_get_url(self, camera_id: str, start: datetime, duration: float,
                         fmt: Literal["fmp4", "mp4"] = "fmp4") -> str:
        if not self._playback_base:
            raise EngineUnavailable(PLAYBACK_DISABLED)
        query = urlencode({"path": mtx_path(camera_id, "main"), "start": format_time(start),
                           "duration": f"{float(duration):.3f}", "format": fmt})
        return f"{self._playback_base}/get?{query}"

    def _view_stream(self, camera_id: str, stream: Literal["main", "sub"]) -> Literal["main", "sub"]:
        src = self._sources.get(camera_id)
        if stream == "sub" and src is not None and not src.sub_url:
            return "main"
        return stream

    def whep_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        if not self._webrtc_base:
            raise EngineUnavailable("La vista en vivo (WebRTC) está desactivada (VMS_MTX_WEBRTC_ADDRESS vacío)")
        return f"{self._webrtc_base}/{mtx_path(camera_id, self._view_stream(camera_id, stream))}/whep"

    def rtsp_read_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        """URL RTSP local SIN credenciales (la configuración de la analítica se guarda en disco).
        Quien la abra añade el usuario de lectura con `vms.core.mtx_auth.with_reader_credentials`."""
        return self.settings.mtx_rtsp_url(mtx_path(camera_id, self._view_stream(camera_id, stream)))

    def http_credentials(self) -> tuple[str, str]:
        """Usuario y contraseña de la API/reproducción/WHEP de MediaMTX (para el proxy del backend)."""
        return self.mtx_credentials.api_auth

    async def disk_usage(self) -> DiskUsage:
        total, used, free = await asyncio.to_thread(disk_guard.disk_usage, Path(self._recordings_dir))
        return DiskUsage(path=self._recordings_dir, total=total, used=used, free=free,
                         percent=round(used / total * 100.0, 1) if total else 0.0)

    # ================================================================== utilidades para pruebas
    @property
    def recordings_dir(self) -> str:
        return self._recordings_dir

    @property
    def paused_devices(self) -> list[CredKey]:
        return list(self._auth.paused)
