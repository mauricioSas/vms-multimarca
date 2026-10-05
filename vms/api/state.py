"""Estado compartido del backend: configuración, usuarios, credenciales, motor y bus de eventos.

La API depende solo de los Protocol de vms.core.interfaces (Engine, DeviceClient), así se prueba
con los dobles de tests/fakes.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from vms import __version__
from vms.core.config_store import ConfigRepository, UserStore
from vms.core.credentials import CredentialStore
from vms.core.errors import EngineUnavailable, VmsError
from vms.core.heartbeat_extras import collect_extras
from vms.core.interfaces import DeviceClient, DeviceTestResult, DiscoveredDevice, Engine, PathStatus
from vms.core.models import AppConfig, Device, DeviceBase, Site
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.core.sources import build_camera_sources

from .events import EventBus
from .security import LoginLimiter, SessionStore

log = logging.getLogger("vms.api")

T = TypeVar("T")
ClientFactory = Callable[[Device, str], DeviceClient]
DeviceTester = Callable[[DeviceBase, str], Awaitable[DeviceTestResult]]
Discoverer = Callable[[float], Awaitable[list[DiscoveredDevice]]]

ANALYTICS_STALE_S = 30.0


@dataclass
class AppState:
    settings: VmsSettings
    paths: AppPaths
    repo: ConfigRepository
    users: UserStore
    creds: CredentialStore
    engine: Engine
    client_factory: ClientFactory
    device_tester: DeviceTester
    discoverer: Discoverer
    internal_token: str
    sessions: SessionStore
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    kiosk_limiter: LoginLimiter = field(default_factory=lambda: LoginLimiter(10, 300.0))
    ip_limiter: LoginLimiter = field(default_factory=lambda: LoginLimiter(20, 300.0))  # fallos por IP
    bus: EventBus = field(default_factory=EventBus)
    started_monotonic: float = field(default_factory=time.monotonic)
    apply_delay: float = 1.0
    snapshot_last: dict[str, float] = field(default_factory=dict)
    proxy: httpx.AsyncClient | None = None
    engine_started: bool = False
    _apply_task: asyncio.Task[None] | None = None
    _apply_dirty: bool = False
    _paths_cache: tuple[float, dict[str, PathStatus]] | None = None
    _engine_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------------ configuración
    def config(self) -> AppConfig:
        return self.repo.config

    async def update_config(self, mutate: Callable[[AppConfig], T], scope: str) -> T:
        """Aplica un cambio (validado y guardado de forma atómica) y lo anuncia por SSE."""
        result = await self.repo.update(mutate)
        self.publish_config(scope)
        return result

    def publish_config(self, scope: str) -> None:
        self.bus.publish("config", {"revision": self.repo.revision, "scope": scope})

    def site(self) -> Site:
        return self.repo.config.settings.site

    def recordings_dir(self, cfg: AppConfig | None = None) -> str:
        cfg = cfg or self.repo.config
        return cfg.settings.recording.recordings_dir or str(self.paths.recordings_dir)

    # ------------------------------------------------------------------ motor
    async def apply_now(self) -> None:
        cfg = self.repo.snapshot()
        sources = await asyncio.to_thread(build_camera_sources, cfg, self.creds)
        await self.engine.apply(sources, cfg.settings.recording, cfg.settings.retention, self.recordings_dir(cfg))
        self._paths_cache = None

    def schedule_apply(self) -> None:
        """Aplica la configuración al motor con antirrebote: varios cambios seguidos = una aplicación."""
        self._apply_dirty = True
        if self._apply_task is None or self._apply_task.done():
            self._apply_task = asyncio.get_running_loop().create_task(self._apply_loop(), name="engine-apply")

    async def _apply_loop(self) -> None:
        while self._apply_dirty:
            await asyncio.sleep(self.apply_delay)
            self._apply_dirty = False
            try:
                await self.apply_now()
            except EngineUnavailable as exc:
                log.warning("No se pudo aplicar la configuración al motor: %s", exc.message)
            except Exception:  # noqa: BLE001 - nunca debe morir en silencio
                log.exception("Error aplicando la configuración al motor de vídeo")

    async def flush_apply(self) -> None:
        """Espera a que termine una aplicación pendiente (útil al parar y en pruebas)."""
        task = self._apply_task
        if task is not None and not task.done():
            await task

    async def start_engine(self) -> bool:
        async with self._engine_lock:
            try:
                await self.engine.start()
                await self.apply_now()
                self.engine_started = True
                return True
            except VmsError as exc:
                log.error("El motor de vídeo no arrancó: %s", exc.message)
            except Exception:  # noqa: BLE001
                log.exception("El motor de vídeo no arrancó")
            return False

    async def paths_status(self, max_age: float = 1.5) -> dict[str, PathStatus]:
        """Estado por ruta con una caché corta (varios muros y paneles piden lo mismo a la vez)."""
        now = time.monotonic()
        if self._paths_cache is not None and now - self._paths_cache[0] < max_age:
            return self._paths_cache[1]
        data = await self.engine.paths_status()
        self._paths_cache = (now, data)
        return data

    async def paths_status_safe(self) -> dict[str, PathStatus] | None:
        try:
            return await self.paths_status()
        except VmsError as exc:
            log.debug("Estado de rutas no disponible: %s", exc.message)
            return None
        except Exception:  # noqa: BLE001
            log.exception("Error leyendo el estado de las rutas")
            return None

    # ------------------------------------------------------------------ analítica
    def analytics_status(self) -> dict[str, Any]:
        f = self.paths.analytics_dir / "status.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"running": False, "stale": True}
        except (OSError, ValueError) as exc:
            log.warning("No se pudo leer el estado de la analítica: %s", exc)
            return {"running": False, "stale": True, "error": "estado ilegible"}
        if not isinstance(data, dict):
            log.warning("El estado de la analítica no tiene el formato esperado")
            return {"running": False, "stale": True, "error": "estado ilegible"}
        stale = True
        try:
            updated = datetime.fromisoformat(str(data.get("updated_at", "")).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            stale = (datetime.now(timezone.utc) - updated).total_seconds() > ANALYTICS_STALE_S
        except ValueError:
            log.debug("Estado de analítica sin fecha válida; se considera desactualizado")
        data["stale"] = stale
        return data

    def analytics_enabled(self) -> bool:
        cfg = self.repo.config
        return any(a.enabled and any(r.enabled for r in cfg.rules_of(a.camera_id)) for a in cfg.analytics_cameras)

    # ------------------------------------------------------------------ salud
    async def overview(self) -> dict[str, Any]:
        """Estado global (lo usan /api/health, /api/status, el latido y SSE)."""
        cfg = self.repo.config
        try:
            engine = await self.engine.status()
        except Exception:  # noqa: BLE001
            log.exception("Error leyendo el estado del motor")
            from vms.core.interfaces import EngineStatus
            engine = EngineStatus(running=False, last_error="Estado del motor no disponible")
        paths = await self.paths_status_safe() if engine.running else None
        disk = None
        try:
            disk = await self.engine.disk_usage()
        except Exception as exc:  # noqa: BLE001
            log.debug("Uso de disco no disponible: %s", exc)
        cams: list[dict[str, Any]] = []
        for cam in cfg.cameras:
            dev = cfg.device(cam.device_id)
            active = cam.enabled and dev is not None and dev.enabled
            main = (paths or {}).get(f"{cam.id}/main")
            sub = (paths or {}).get(f"{cam.id}/sub")
            cams.append({
                "camera_id": cam.id, "name": cam.name, "enabled": active,
                "online": bool(main and main.ready) if active and paths is not None else (None if active else False),
                "recording": bool(main and main.recording) if active and paths is not None else False,
                "readers": (main.readers if main else 0) + (sub.readers if sub else 0),
                "bytes_received": main.bytes_received if main else 0,
                "last_error": (main.last_error if main else "") if active else "",
            })
        analytics = self.analytics_status()
        problems = []
        if not engine.running:
            status = "down"
        else:
            if any(c["enabled"] and c["online"] is False for c in cams):
                problems.append("cámaras sin vídeo")
            guard = cfg.settings.retention.disk_guard_percent
            if disk is not None and guard and disk.percent > guard:
                problems.append("disco por encima del umbral")
            if self.analytics_enabled() and (not analytics.get("running") or analytics.get("stale")):
                problems.append("analítica sin estado reciente")
            db_raw = analytics.get("db")
            db: dict[str, Any] = db_raw if isinstance(db_raw, dict) else {}
            if analytics.get("running") and db.get("disk_error"):
                problems.append("analítica sin espacio en disco para la cola de conteos")
            status = "degraded" if problems else "ok"
        return {"status": status, "problems": problems, "engine": engine, "disk": disk, "cameras": cams,
                "analytics": analytics}

    def uptime_s(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 1)

    async def heartbeat_payload(self) -> dict[str, Any]:
        ov = await self.overview()
        cams = [c for c in ov["cameras"] if c["enabled"]]
        disk = ov["disk"]
        engine = ov["engine"]
        return {
            "version": __version__, "hostname": socket.gethostname(), "uptime_s": int(self.uptime_s()),
            "status": ov["status"],
            "engine": {"running": engine.running, "restarts": engine.restarts},
            "cameras": [{"camera_id": c["camera_id"], "name": c["name"], "online": bool(c["online"]),
                         "recording": bool(c["recording"])} for c in cams],
            "cameras_total": len(cams), "cameras_online": sum(1 for c in cams if c["online"]),
            "disk": {"percent": disk.percent, "free_gb": round(disk.free / 1e9, 1)} if disk else {},
            "analytics": {"running": bool(ov["analytics"].get("running")),
                          "stale": bool(ov["analytics"].get("stale", True))},
            # update (B4), health y evidence_key (B6): cada bloque aporta su proveedor (heartbeat_extras)
            **await asyncio.to_thread(collect_extras, self.paths),
        }
