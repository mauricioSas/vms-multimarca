"""MediaMtxEngine: implementación de vms.core.interfaces.Engine sobre MediaMTX (CONTRATO §4 y §5.2).

Responsabilidades:
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
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlencode, urlsplit

from vms.core.errors import EngineUnavailable
from vms.core.interfaces import CameraSource, DiskUsage, EngineStatus, PathStatus, RecordingSpan
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.mtx_auth import MtxCredentials
from vms.core.naming import mtx_path, parse_mtx_path
from vms.core.paths import AppPaths, find_mediamtx
from vms.core.settings import VmsSettings

from . import disk_guard
from .mtx_api import MediaMtxApi, MtxApiError, format_time
from .mtx_config import (conf_hash, global_config, http_base, path_configs, path_defaults, split_address,
                         write_config)
from .process import BACKOFF, MediaMtxProcess

log = logging.getLogger("vms.engine")
mtx_log = logging.getLogger("vms.engine.mediamtx")

_LINE_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (DEB|INF|WAR|ERR) (.*)$")
_PATH_RE = re.compile(r"^\[path ([^\]]+)\] (.*)$")
_SOURCE_RE = re.compile(r"^\[[^\]]*source\] (.*)$")
_LEVELS = {"DEB": logging.DEBUG, "INF": logging.INFO, "WAR": logging.WARNING, "ERR": logging.ERROR}
_QUIET = ("reloading configuration (API request)", "[API] path not found", "[API] path already exists")
REPEAT_SILENCE_S = 300.0
AUTH_PAUSED_MSG = ("Contraseña rechazada por el equipo: se pausó la conexión para que no bloquee el usuario. "
                   "Corrige la contraseña del equipo; mientras tanto se reintenta sola de vez en cuando.")


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


class MediaMtxEngine:
    def __init__(self, settings: VmsSettings, paths: AppPaths, *, mediamtx_bin: Path | None = None,
                 disk_guard_interval: float = 60.0, watchdog_interval: float = 15.0,
                 auth_fail_threshold: int = 2, auth_pause_seconds: float = 1800.0,
                 backoff: tuple[float, ...] = BACKOFF, api_timeout: float = 5.0) -> None:
        self.settings = settings
        self.paths = paths
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
        self.backoff = backoff
        self.config_file = paths.mediamtx_dir / "mediamtx.yml"

        self._recording = RecordingSettings()
        self._retention = RetentionSettings()
        self._recordings_dir = str(paths.recordings_dir)
        self._sources: dict[str, CameraSource] = {}
        self._applied: dict[str, str] = {}          # ruta → huella de la conf registrada en MediaMTX
        self._path_errors: dict[str, str] = {}
        self._log_seen: dict[tuple[str, str], float] = {}
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
                       asyncio.create_task(self._watchdog_loop(), name="engine-watchdog")]
        log.info("Motor de vídeo en marcha (MediaMTX %s)", self._version or "?")

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
                # El YAML NO se reescribe aquí: MediaMTX recargaría el archivo y perdería las rutas
                # registradas por la API. Se regenera antes de cada arranque (before_spawn) y en
                # caliente se cambia por la API.
                Path(self._recordings_dir).mkdir(parents=True, exist_ok=True)
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

    async def _sync_locked(self) -> None:
        """Alinea las rutas de MediaMTX con las cámaras deseadas (requiere self._lock)."""
        if self.api is None or not self.running:
            raise EngineUnavailable("El motor de vídeo no está en marcha")
        desired = self._desired()
        present = await self.api.config_path_names()
        for name in sorted(present):
            if parse_mtx_path(name) is None:
                continue
            conf = desired.get(name)
            if conf is None or self._auth.is_paused(conf["source"]):
                if await self.api.delete_path(name):
                    log.info("Ruta %s retirada del motor", name)
                self._applied.pop(name, None)
        present = {n for n in present if n in desired and not self._auth.is_paused(desired[n]["source"])}
        for name, conf in desired.items():
            if self._auth.is_paused(conf["source"]):
                self._path_errors[name] = AUTH_PAUSED_MSG
                continue
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
    def _on_line(self, line: str) -> None:
        m = _LINE_RE.match(line)
        if not m:
            mtx_log.info("%s", line)
            return
        level, msg = _LEVELS[m.group(1)], m.group(2)
        pm = _PATH_RE.match(msg)
        if pm:
            name, rest = pm.group(1), pm.group(2)
            sm = _SOURCE_RE.match(rest)
            if sm and m.group(1) in ("ERR", "WAR"):
                raw = sm.group(1)
                self._path_errors[name] = explain_source_error(raw)
                if is_auth_error(raw) and name in self._applied:
                    self._on_auth_failure(name)
                key = (name, raw)
                now = time.monotonic()
                if now - self._log_seen.get(key, -1e9) < REPEAT_SILENCE_S:
                    level = logging.DEBUG  # el mismo error cada 5 s no debe inundar el registro
                else:
                    self._log_seen[key] = now
                    if len(self._log_seen) > 2000:
                        self._log_seen.clear()
            elif "is available and online" in rest or "is ready" in rest:
                if name in self._path_errors and self._path_errors[name] != AUTH_PAUSED_MSG:
                    self._path_errors.pop(name, None)
                parsed = parse_mtx_path(name)
                src = self._sources.get(parsed[0]) if parsed else None
                if src is not None:
                    self._auth.success(src.main_url)
        if any(q in msg for q in _QUIET):
            level = logging.DEBUG
        mtx_log.log(level, "%s", msg)

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

    async def _retry_paused(self) -> None:
        from vms.vendors.rtsp_probe import probe_rtsp

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
                for name, msg in list(self._path_errors.items()):
                    if msg == AUTH_PAUSED_MSG:
                        self._path_errors.pop(name, None)

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
            out[name] = PathStatus(
                name=name, camera_id=parsed[0], stream=parsed[1],  # type: ignore[arg-type]
                ready=ready, source_online=ready, readers=len(item.get("readers") or []),
                bytes_received=int(item.get("inboundBytes") or item.get("bytesReceived") or 0),
                tracks=[str(t) for t in (item.get("tracks") or [])],
                recording=ready and bool(conf.get("record")),
                last_error="" if ready else self._path_errors.get(name, ""))
        for name in desired:
            if name not in out:
                parsed = parse_mtx_path(name)
                assert parsed is not None
                out[name] = PathStatus(name=name, camera_id=parsed[0], stream=parsed[1],  # type: ignore[arg-type]
                                       last_error=self._path_errors.get(name, ""))
        return out

    async def list_recordings(self, camera_id: str, start: datetime | None,
                              end: datetime | None) -> list[RecordingSpan]:
        if self.api is None:
            raise EngineUnavailable("El motor de vídeo no está en marcha")
        return await self.api.list_recordings(mtx_path(camera_id, "main"), start, end)

    def playback_get_url(self, camera_id: str, start: datetime, duration: float,
                         fmt: Literal["fmp4", "mp4"] = "fmp4") -> str:
        if not self._playback_base:
            raise EngineUnavailable("La reproducción está desactivada (VMS_MTX_PLAYBACK_ADDRESS vacío)")
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
