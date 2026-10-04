"""Latido (heartbeat) de las sedes: escritura en PostgreSQL.

Dos formas de que una sede informe de su salud, que escriben exactamente lo mismo:

1. `HeartbeatSender` (docs/CONTRATO.md §7.2): el backend VMS de la tienda escribe directamente
   en la PostgreSQL central por la VPN. Lo arranca el backend si hay `VMS_PG_DSN`.
2. El agente HTTP (`python -m central.agent`) envía el estado al panel central
   (`POST /api/heartbeat`, token por sede) y es el panel quien escribe. Sirve para sedes que no
   tienen acceso directo a la base de datos.

En ambos casos `record_heartbeat()` hace, en una transacción: upsert de `sites`, de
`site_cameras` (desde `payload.cameras`) y de `site_heartbeats`. `last_seen` es la hora del
servidor de base de datos (no la del PC de la tienda, que puede ir desfasada).

RGPD: el latido solo contiene salud técnica (versión, cámaras con vídeo, disco, temperatura).
Nada de imágenes ni datos de personas.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from vms.core.models import Site
from vms.core.naming import ID_PATTERN
from vms.core.rtsp import redact

log = logging.getLogger("central.heartbeat")

HeartbeatStatus = Literal["ok", "degraded", "down"]
MAX_CAMERAS = 512
MAX_PAYLOAD_BYTES = 128 * 1024


# =========================================================================== modelos
class HeartbeatCamera(BaseModel):
    model_config = ConfigDict(extra="ignore")

    camera_id: str = Field(pattern=ID_PATTERN)
    name: str = Field("", max_length=80)
    online: bool | None = None
    recording: bool | None = None


class HeartbeatPayload(BaseModel):
    """Contenido del latido (CONTRATO §7.3). Campos extra permitidos para ampliar sin romper."""

    model_config = ConfigDict(extra="allow")

    version: str = Field("", max_length=40)
    hostname: str = Field("", max_length=120)
    uptime_s: float | None = Field(None, ge=0)
    status: HeartbeatStatus = "ok"
    engine: dict[str, Any] = Field(default_factory=dict)
    cameras: list[HeartbeatCamera] = Field(default_factory=list, max_length=MAX_CAMERAS)
    cameras_total: int | None = Field(None, ge=0)
    cameras_online: int | None = Field(None, ge=0)
    disk: dict[str, Any] = Field(default_factory=dict)
    analytics: dict[str, Any] = Field(default_factory=dict)
    temperature_c: float | None = Field(None, ge=-50, le=150)
    interval_s: int | None = Field(None, ge=10, le=3600)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: object) -> object:
        return v if v in ("ok", "degraded", "down") else "degraded"


class SiteInfo(BaseModel):
    """Datos de la sede que viajan con el latido. `name` vacío = no cambiar el guardado."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field("", max_length=80)
    code: str = Field("", max_length=32)
    timezone: str = Field("", max_length=64)


class HeartbeatIn(BaseModel):
    """Cuerpo de `POST /api/heartbeat` (agente HTTP)."""

    model_config = ConfigDict(extra="forbid")

    site: SiteInfo
    payload: HeartbeatPayload
    sent_at: datetime | None = None


# =========================================================================== escritura
_SITE_SQL = """
INSERT INTO sites (site_id, name, code, timezone)
VALUES (%(site_id)s, %(name)s, %(code)s, %(timezone)s)
ON CONFLICT (site_id) DO UPDATE SET
    name       = CASE WHEN %(has_name)s THEN EXCLUDED.name ELSE sites.name END,
    code       = CASE WHEN %(has_code)s THEN EXCLUDED.code ELSE sites.code END,
    timezone   = CASE WHEN %(has_tz)s THEN EXCLUDED.timezone ELSE sites.timezone END,
    updated_at = now()
"""

_CAMERA_SQL = """
INSERT INTO site_cameras (site_id, camera_id, name)
VALUES (%s, %s, %s)
ON CONFLICT (site_id, camera_id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
"""

_HEARTBEAT_SQL = """
INSERT INTO site_heartbeats (site_id, last_seen, hostname, version, status, payload)
VALUES (%(site_id)s, now(), %(hostname)s, %(version)s, %(status)s, %(payload)s)
ON CONFLICT (site_id) DO UPDATE SET
    last_seen = EXCLUDED.last_seen,
    hostname  = EXCLUDED.hostname,
    version   = EXCLUDED.version,
    status    = EXCLUDED.status,
    payload   = EXCLUDED.payload
"""


_TZ_SQL = "SELECT 1 FROM pg_timezone_names WHERE name = %s"


def _site_params(site: SiteInfo, tz_ok: bool) -> dict[str, Any]:
    return {
        "site_id": site.id, "name": site.name or site.id, "code": site.code,
        "timezone": site.timezone if tz_ok else "Europe/Madrid",
        "has_name": bool(site.name), "has_code": bool(site.code), "has_tz": tz_ok,
    }


def _heartbeat_params(site: SiteInfo, payload: HeartbeatPayload) -> dict[str, Any]:
    return {"site_id": site.id, "hostname": payload.hostname, "version": payload.version,
            "status": payload.status, "payload": Jsonb(payload.model_dump(mode="json"))}


def _camera_rows(site: SiteInfo, payload: HeartbeatPayload) -> list[tuple[str, str, str]]:
    return [(site.id, c.camera_id, c.name or c.camera_id) for c in payload.cameras]


def _warn_tz(site: SiteInfo, tz_ok: bool) -> None:
    if site.timezone and not tz_ok:
        log.warning("Sede %s: zona horaria desconocida «%s»; se mantiene la guardada", site.id, site.timezone)


async def _valid_timezone(conn: psycopg.AsyncConnection[Any], tz: str) -> bool:
    if not tz:
        return False
    cur = await conn.execute(_TZ_SQL, (tz,))
    return await cur.fetchone() is not None


async def record_heartbeat(conn: psycopg.AsyncConnection[Any], site: SiteInfo,
                           payload: HeartbeatPayload) -> None:
    """Guarda un latido en una transacción (sites + site_cameras + site_heartbeats)."""
    tz_ok = await _valid_timezone(conn, site.timezone)
    _warn_tz(site, tz_ok)
    async with conn.transaction():
        await conn.execute(_SITE_SQL, _site_params(site, tz_ok))
        cams = _camera_rows(site, payload)
        if cams:
            async with conn.cursor() as cur:
                await cur.executemany(_CAMERA_SQL, cams)
        await conn.execute(_HEARTBEAT_SQL, _heartbeat_params(site, payload))


def record_heartbeat_sync(conn: psycopg.Connection[Any], site: SiteInfo, payload: HeartbeatPayload) -> None:
    """Igual que `record_heartbeat`, con una conexión síncrona (la usa el backend VMS en un hilo)."""
    tz_ok = bool(site.timezone) and conn.execute(_TZ_SQL, (site.timezone,)).fetchone() is not None
    _warn_tz(site, tz_ok)
    with conn.transaction():
        conn.execute(_SITE_SQL, _site_params(site, tz_ok))
        cams = _camera_rows(site, payload)
        if cams:
            with conn.cursor() as cur:
                cur.executemany(_CAMERA_SQL, cams)
        conn.execute(_HEARTBEAT_SQL, _heartbeat_params(site, payload))


# =========================================================================== emisor directo (§7.2)
class HeartbeatSender:
    """Tarea en segundo plano que escribe el latido de esta sede en la PostgreSQL central.

    `collect()` (lo aporta el backend) devuelve el payload de CONTRATO §7.3. Si PostgreSQL no
    responde, se registra un aviso y se reintenta en el siguiente ciclo, sin acumular latidos.
    `start()` y `stop()` nunca lanzan hacia fuera.
    """

    def __init__(self, dsn: str, site: Site | Callable[[], Site], interval_s: int,
                 collect: Callable[[], Awaitable[dict[str, Any]]], *,
                 connect_timeout: float = 10.0) -> None:
        self._dsn = dsn
        self._site = site
        self._interval = max(1, int(interval_s))
        self._collect = collect
        self._connect_timeout = connect_timeout
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.sent = 0
        self.failures = 0
        self.last_error = ""

    @property
    def site(self) -> Site:
        """Sede actual. Si se pasó una función, se consulta en cada latido: así un cambio de nombre o de
        zona horaria hecho desde el panel llega a la central sin reiniciar el backend."""
        return self._site() if callable(self._site) else self._site

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="central-heartbeat")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=self._connect_timeout + 5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except Exception:
            log.exception("Error al detener el latido")

    async def send_once(self) -> bool:
        """Recoge y escribe un latido. Devuelve True si se guardó."""
        try:
            raw = await self._collect()
            payload = HeartbeatPayload.model_validate(raw)
            if not payload.hostname:
                payload.hostname = socket.gethostname()
            if payload.interval_s is None:
                payload.interval_s = max(10, self._interval)
            cur = self.site
            site = SiteInfo(id=cur.id, name=cur.name, code=cur.code, timezone=cur.timezone)
            # psycopg síncrono en un hilo: el backend corre en el bucle Proactor de Windows (lo necesita
            # para lanzar MediaMTX) y psycopg asíncrono no funciona con ese bucle.
            await asyncio.to_thread(self._write, site, payload)
        except Exception as exc:  # nunca se propaga: el latido no debe tumbar el backend
            self.failures += 1
            self.last_error = redact(str(exc))[:500]
            log.warning("No se pudo guardar el latido de la sede %s: %s", self.site.id, self.last_error)
            return False
        self.sent += 1
        self.last_error = ""
        return True

    def _write(self, site: SiteInfo, payload: HeartbeatPayload) -> None:
        with psycopg.connect(self._dsn, connect_timeout=int(self._connect_timeout),
                             application_name="vms-heartbeat") as conn:
            record_heartbeat_sync(conn, site, payload)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            started = loop.time()
            await self.send_once()
            wait = max(0.0, self._interval - (loop.time() - started))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
