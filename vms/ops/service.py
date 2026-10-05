"""Servicio de B6 dentro del backend: tareas en segundo plano y operaciones que usan las rutas.

Lo crea el `lifespan` del router `health` (los `lifespan` de los routers se encadenan al de la aplicación,
así no se toca `vms/api/app.py`) y queda en `app.state.ops`. Tareas:

- **salud de imagen**: una comprobación por cámara cada `settings.health.check_interval_s` (180 s),
  repartidas en el intervalo, con `asyncio.to_thread` (nunca decodifica de forma continua);
- **hora**: cada hora, desfase de cada equipo (capacidad `time_read`) y del PC;
- **vigilancia**: cada 30 s, cámaras sin vídeo (avisos agrupados) y, cada 10 min, el resumen del informe
  de salud para el latido (`ops/health-summary.json`) y la previsión de grabación;
- **mantenimiento**: cada hora, caducidad de tramos protegidos (y reintento de borrar copias que no se
  pudieron borrar), caducidad de los paquetes exportados (`export_retention_days`, 30 por defecto), limpieza
  de la base (90/400 días) y borrado de las referencias de cámaras que ya no existen (RGPD).

Al arrancar: crea la clave de firma de evidencias (así el latido lleva `evidence_key` desde el primer día) y
da por fallidas las exportaciones que quedaron a medias (el proceso se cortó) borrando sus restos.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from vms import __version__
from vms.core.audit import audit
from vms.core.errors import DeviceAuthFailed, EngineUnavailable, NotFoundError, VmsError
from vms.core.interfaces import DeviceClockClient
from vms.core.models import Camera, Device

from . import drivers
from .diagnose import DiagnoseInput, Diagnoser, llm_rewriter_from_settings
from .evidence.bookmarks import BookmarkManager
from .evidence.export import CameraInfo, EvidenceBuilder, ExportContext, new_export_id
from .evidence.keys import EvidenceKey, load_or_create
from .health import clock as clockmod
from .health import imaging
from .health.forecast import forecast as compute_forecast
from .health.forecast import mbps_to_bytes_per_hour
from .health.references import ReferenceStore
from .health.snapshot import AuthBackoff, grab
from .health.tracker import HealthTracker, Transition
from .host import EVENT_BOOKMARK, EVENT_EVIDENCE, EVENT_HEALTH, EVENT_NOTICE, OpsHost
from .models import (CameraHealth, ClockCheck, HealthCause, HealthMetrics, DiagnosisResult, Severity, EvidenceExport,
                     EvidenceExportRequest, HealthCheck, HealthReport, RetentionForecast, SecurityAuditReport,
                     TimelineEvent)
from .notify import Alert, Notifier
from .onboarding import OnboardingStore
from .report import CameraInput, build, day_bounds, gaps, read_smart, site_tz, write_summary
from .security.advisories import load_table
from .security.audit import AuditDeps, run_audit
from .drivers import TIME_READ
from .store import OpsStore

log = logging.getLogger("vms.ops")

REFERENCE_FRAMES = 15
REFERENCE_INTERVAL_S = 12.0           # 15 fotogramas en unos 3 minutos
CAMERA_DOWN_POLLS = 2                 # 2 sondeos de 30 s sin vídeo = aviso
MONITOR_PERIOD_S = 30.0
SUMMARY_PERIOD_S = 600.0
CLOCK_PERIOD_S = 3600.0
MAINTENANCE_PERIOD_S = 3600.0
EXPORT_RETENTION_DEFAULT_DAYS = 30
EXPORT_RETENTION_KEY = "export_retention_days"
PC_SNTP_KEY = "pc_sntp_enabled"
RECORDING_GAP_ALERT_MIN = 5.0         # una cámara con vídeo que lleva 5 min sin grabar = aviso
DISK_LOW_FREE_BYTES = 2 * 10**9       # menos de 2 GB libres: la grabación está a punto de pararse
RECOVERY_KINDS = {"camera_down": "camera_up", "tamper": "tamper_cleared", "recording_gap": "recording_gap_cleared"}


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class OpsService:
    def __init__(self, state: OpsHost, *, initial_delay: float = 45.0,
                 reference_interval_s: float = REFERENCE_INTERVAL_S, reference_frames: int = REFERENCE_FRAMES) -> None:
        self.state = state
        base = state.paths.base
        self.ops_dir = base / "ops"
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        self.store = OpsStore(self.ops_dir / "ops.sqlite3")
        self.refs = ReferenceStore(self.ops_dir / "references")
        self.evidence_dir = base / "evidence"
        self.protected_dir = self.evidence_dir / "protected"
        self.exports_dir = self.evidence_dir / "exports"
        cfg = state.config()
        self.tracker = HealthTracker(cfg.settings.health.hysteresis)
        for cam in cfg.cameras:
            saved = self.store.kv_get(f"health_state:{cam.id}")
            if isinstance(saved, dict):
                try:
                    self.tracker.restore(cam.id, CameraHealth.model_validate(saved))
                except ValueError:
                    log.debug("Estado de salud guardado no válido para %s", cam.id)
        self.bookmarks = BookmarkManager(self.store, self.protected_dir, lambda: state.recordings_dir(),
                                         lambda: state.config().settings.recording.segment_seconds)
        self.builder = EvidenceBuilder(self.exports_dir)
        self.notifier = Notifier(self.store, state.creds, lambda: state.config().settings.notifications, state.site,
                                 telegram=self._telegram, publish_notice=self._notice)
        self.onboarding = OnboardingStore(self.ops_dir / "onboarding.json")
        self.initial_delay = initial_delay
        self.reference_interval_s = reference_interval_s
        self.reference_frames = reference_frames
        self.jobs: dict[str, dict[str, Any]] = {}
        self._prev: dict[str, tuple[float, imaging.Image]] = {}      # último fotograma (solo en memoria)
        self._down: dict[str, int] = {}
        self._down_alerted: set[str] = set()
        self._last_forecast_alert: date | None = None
        self._disk_alerted: dict[str, date] = {}
        self._gap_alerted: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[Any]] = set()
        self._key: EvidenceKey | None = None
        self._check_locks: dict[str, asyncio.Lock] = {}
        self.auth_backoff = AuthBackoff()

    # ================================================================== ciclo de vida
    async def start(self) -> None:
        try:
            await asyncio.to_thread(self.key)        # el latido lleva evidence_key desde el primer día
        except Exception:  # noqa: BLE001 - sin clave no se exporta, pero el resto de B6 funciona
            log.exception("No se pudo preparar la clave de firma de evidencias")
        await asyncio.to_thread(self.recover_exports)
        loop = asyncio.get_running_loop()
        for name, coro in (("ops-health", self._health_loop()), ("ops-clock", self._clock_loop()),
                           ("ops-monitor", self._monitor_loop()), ("ops-maintenance", self._maintenance_loop())):
            self._tasks.append(loop.create_task(coro, name=name))

    async def stop(self) -> None:
        for t in self._tasks + list(self._background):
            t.cancel()
        for t in self._tasks + list(self._background):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - al parar solo se registra
                pass
        self._tasks.clear()
        try:
            await asyncio.wait_for(self.notifier.flush_all(), 10.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.debug("Avisos agrupados descartados al parar", exc_info=True)
        await self.notifier.close()
        self.store.close()

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _telegram(self) -> tuple[str, str] | None:
        cfg = self.state.config().settings.alerts
        token = self.state.settings.telegram_bot_token
        if not cfg.telegram_enabled or not cfg.telegram_chat_id or token is None:
            return None
        return token.get_secret_value(), cfg.telegram_chat_id

    def _notice(self, data: dict[str, Any]) -> None:
        # `camera_ids` (aditivo a NoticeEvent, petición CONTRATO §12): para que el SSE filtre por ámbito
        self.state.bus.publish(EVENT_NOTICE, {"severity": data["severity"], "kind": data["kind"],
                                              "title_es": data["title_es"], "count": int(data.get("count", 1)),
                                              "camera_ids": [str(c) for c in data.get("camera_ids") or []]})

    # ================================================================== salud de imagen
    def _camera(self, camera_id: str) -> Camera:
        cam = self.state.config().camera(camera_id)
        if cam is None:
            raise NotFoundError("La cámara no existe")
        return cam

    def camera_health(self, camera_id: str) -> CameraHealth:
        h = self.tracker.get(camera_id)
        h.references = self.refs.dates(camera_id)
        cam = self.state.config().camera(camera_id)
        if cam is not None:
            h.clock = self.store.latest_clock_checks().get(cam.device_id)
        if not h.references and h.status == "unknown":
            h.causes = [HealthCause.NO_REFERENCE]
        return h

    def all_health(self, camera_ids: list[str]) -> list[CameraHealth]:
        clocks = self.store.latest_clock_checks()
        cfg = self.state.config()
        out = []
        for cid in camera_ids:
            h = self.tracker.get(cid)
            h.references = self.refs.dates(cid)
            cam = cfg.camera(cid)
            if cam is not None:
                h.clock = clocks.get(cam.device_id)
            if not h.references and h.status == "unknown":
                h.causes = [HealthCause.NO_REFERENCE]
            out.append(h)
        return out

    async def check_camera(self, camera_id: str, frame: imaging.Image | None = None) -> HealthCheck:
        """Comprobación inmediata (también la usa la tarea periódica). `frame` solo en pruebas."""
        cam = self._camera(camera_id)
        lock = self._check_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            if frame is None:
                frame = await grab(self.state, cam, self.auth_backoff)
            masks = [list(p) for p in cam.health.masks]
            if frame is None:
                check = HealthCheck(camera_id=camera_id, status="unknown", causes=[HealthCause.NO_SNAPSHOT])
            else:
                day = await asyncio.to_thread(self.refs.load, camera_id, "day", masks)
                night = await asyncio.to_thread(self.refs.load, camera_id, "night", masks)
                prev = self._prev.get(camera_id)
                previous = prev[1] if prev is not None and time.monotonic() - prev[0] >= 20 else None
                analysis = await asyncio.to_thread(imaging.analyze, frame, day=day, night=night, previous=previous,
                                                   masks=masks)
                check = imaging.to_check(camera_id, analysis)
                self._prev[camera_id] = (time.monotonic(), imaging.to_work(frame))
            await asyncio.to_thread(self.store.add_health_check, check)
            self.tracker.hysteresis = self.state.config().settings.health.hysteresis
            transition = self.tracker.update(check)
            if transition is not None:
                await self._on_transition(cam, transition, frame)
            self.store.kv_set(f"health_state:{camera_id}",
                              self.tracker.get(camera_id).model_dump(mode="json", exclude={"last_check"}))
            return check

    async def _on_transition(self, cam: Camera, tr: Transition, frame: imaging.Image | None) -> None:
        h = tr.health
        clock = self.store.latest_clock_checks().get(cam.device_id)
        self.state.bus.publish(EVENT_HEALTH, {"camera_id": cam.id, "score": h.score, "status": h.status,
                                        "causes": [str(c) for c in h.causes],
                                        "clock_skew_s": clock.skew_s if clock else None})
        now = datetime.now(timezone.utc)
        bad = h.status in ("warning", "critical")
        was_bad = tr.previous in ("warning", "critical")
        if was_bad:
            self.store.close_open_timeline_event(cam.id, "health", now)
        if bad:
            texts = imaging.describe_causes(list(h.causes), h.last_check.metrics if h.last_check else
                                            HealthMetrics())
            title = f"{texts[0] if texts else 'Problema de imagen'}: {cam.name}"
            sev: Severity = "critical" if h.status == "critical" else "warning"
            self.store.add_timeline_event(TimelineEvent(camera_id=cam.id, layer="health", start=now,
                                                        severity=sev, title_es=title))
            if h.status == "critical" and frame is not None:
                await asyncio.to_thread(self.refs.save_alert, cam.id, imaging.to_work(frame))
            await self.notifier.emit(Alert(kind="tamper", severity=sev, title_es=title,
                                           camera_id=cam.id, camera_name=cam.name,
                                           details={"score": h.score, "causes": [str(c) for c in h.causes]}))
        elif was_bad and h.status == "ok":
            self.refs.clear_alert(cam.id)
            await self.notifier.emit(Alert(kind="tamper_cleared", severity="info",
                                           title_es=f"Imagen recuperada: {cam.name}", camera_id=cam.id,
                                           camera_name=cam.name, details={"score": h.score}))

    async def _health_loop(self) -> None:
        await asyncio.sleep(self.initial_delay)
        while True:
            cfg = self.state.config()
            hs = cfg.settings.health
            cams = [c for c in cfg.cameras if c.enabled and c.health.enabled and self._device_enabled(c)]
            if not hs.enabled or not cams:
                await asyncio.sleep(60)
                continue
            gap = hs.check_interval_s / len(cams)
            for cam in cams:
                started = time.monotonic()
                if self.refs.dates(cam.id):
                    try:
                        await self.check_camera(cam.id)
                    except NotFoundError:
                        pass
                    except Exception:  # noqa: BLE001 - una cámara rara no para la vigilancia
                        log.exception("Error comprobando la salud de %s", cam.id)
                await asyncio.sleep(max(1.0, gap - (time.monotonic() - started)))

    def _device_enabled(self, cam: Camera) -> bool:
        dev = self.state.config().device(cam.device_id)
        return dev is not None and dev.enabled

    # ---------------------------------------------------------------- referencias
    def start_reference_job(self, camera_id: str, kind: Literal["day", "night"], user: str) -> str:
        cam = self._camera(camera_id)
        for job in self.jobs.values():
            if job["camera_id"] == camera_id and job["state"] == "running":
                return str(job["job_id"])
        job_id = f"ref-{secrets.token_hex(4)}"
        self.jobs[job_id] = {"job_id": job_id, "camera_id": camera_id, "kind": kind, "state": "running",
                             "frames": 0, "total": self.reference_frames, "message_es": "Tomando fotogramas…",
                             "person_warning": False}
        self._spawn(self._reference_job(job_id, cam, kind, user), f"ops-reference-{camera_id}")
        return job_id

    async def _reference_job(self, job_id: str, cam: Camera, kind: Literal["day", "night"], user: str) -> None:
        job = self.jobs[job_id]
        frames: list[imaging.Image] = []
        try:
            for i in range(self.reference_frames):
                img = await grab(self.state, cam, self.auth_backoff)
                if img is not None:
                    frames.append(imaging.to_work(img))
                job["frames"] = len(frames)
                if i < self.reference_frames - 1:
                    await asyncio.sleep(self.reference_interval_s)
            if len(frames) < 5:
                raise RuntimeError("La cámara no dio imagen suficientes veces: comprueba que tiene vídeo")
            built = await asyncio.to_thread(imaging.build_reference, frames)
            await asyncio.to_thread(self.refs.save, cam.id, kind, built.image, user=user, frames=built.frames,
                                    activity=built.activity_ratio)
            frames.clear()
            self.tracker.forget(cam.id)
            self._prev.pop(cam.id, None)
            self.store.kv_set(f"health_state:{cam.id}", None)
            job.update(state="done", person_warning=built.person_warning,
                       message_es=("Referencia fijada. Hubo movimiento mientras se tomaba: si alguien se quedó "
                                   "quieto puede aparecer en ella; repítela con la tienda cerrada."
                                   if built.person_warning else "Referencia fijada."))
            audit("health_reference_set", user=user, ip="", camera_id=cam.id, camera=cam.name, kind=kind,
                  frames=built.frames)
            self.state.bus.publish(EVENT_HEALTH, {"camera_id": cam.id, "score": None, "status": "unknown",
                                            "causes": [], "clock_skew_s": None})
            self._spawn(self._first_check(cam.id), f"ops-first-check-{cam.id}")
        except Exception as exc:  # noqa: BLE001
            job.update(state="failed", message_es=str(exc) if isinstance(exc, RuntimeError) else
                       "No se pudo fijar la referencia; revisa el registro del sistema")
            if not isinstance(exc, RuntimeError):
                log.exception("Error fijando la referencia de %s", cam.id)

    async def _first_check(self, camera_id: str) -> None:
        try:
            await self.check_camera(camera_id)
        except Exception:  # noqa: BLE001
            log.debug("Primera comprobación tras fijar la referencia fallida", exc_info=True)

    async def current_jpeg(self, camera_id: str) -> bytes:
        img = await grab(self.state, self._camera(camera_id), self.auth_backoff)
        if img is None:
            raise VmsError("No se pudo obtener una imagen de la cámara", code="device_error", status=502)
        return imaging.encode_jpeg(imaging.to_work(img), 85)

    # ================================================================== hora
    async def clock_check_all(self) -> tuple[ClockCheck, list[ClockCheck]]:
        cfg = self.state.config()
        hs = cfg.settings.health
        pc = await asyncio.to_thread(lambda: clockmod.pc_check(hs.clock_warn_s, hs.clock_critical_s,
                                                               sntp=self.pc_sntp_enabled()))
        await asyncio.to_thread(self.store.add_clock_check, pc)
        out: list[ClockCheck] = []
        for dev in cfg.devices:
            if not dev.enabled:
                continue
            chk = await self._device_clock(dev, hs.clock_warn_s, hs.clock_critical_s)
            await asyncio.to_thread(self.store.add_clock_check, chk)
            out.append(chk)
            if chk.status in ("warning", "critical"):
                now = datetime.now(timezone.utc)
                for cam in cfg.cameras_of(dev.id):
                    self.store.add_timeline_event(TimelineEvent(camera_id=cam.id, layer="clock", start=now,
                                                                severity=chk.status,
                                                                title_es=chk.message_es[:200]))
                if chk.status == "critical":
                    await self.notifier.emit(Alert(kind="clock_skew", severity="critical",
                                                   title_es=f"Hora desajustada: {dev.name}",
                                                   details={"skew_s": chk.skew_s, "time_mode": chk.time_mode}))
        return pc, out

    async def _device_clock(self, dev: Device, warn: float, critical: float) -> ClockCheck:
        """Hora de un equipo. Nunca lanza: un equipo sin API, con otra marca o con la contraseña mala da un
        resultado «Desconocido» y el bucle sigue con los demás."""
        cams = self.state.config().cameras_of(dev.id)
        cam_id = cams[0].id if len(cams) == 1 else None
        if TIME_READ not in drivers.capabilities(dev.vendor) or not drivers.has_api(dev.vendor):
            return clockmod.failed_check(dev.id, cam_id, f"Los equipos «{drivers.driver_name(dev.vendor)}» no "
                                                         "permiten leer la hora desde el programa.")
        password = self.state.creds.get_device_password(dev.id)
        if self.auth_backoff.blocked(dev.id, password):
            return clockmod.failed_check(dev.id, cam_id, "El equipo rechazó la contraseña guardada: no se vuelve a "
                                                         "probar hasta que la cambies (o en 30 minutos) para no "
                                                         "bloquear el usuario.")
        client: Any = None
        try:
            client = self.state.client_factory(dev, password)
            if not isinstance(client, DeviceClockClient):
                return clockmod.failed_check(dev.id, cam_id, "Esta versión aún no sabe leer la hora de esta marca.")
            dt = await client.device_time()
            if dt.credentials_rejected:
                self.auth_backoff.block(dev.id, password)   # la hora vale, pero no se vuelve a gastar un intento
            return clockmod.device_check(dev.id, cam_id, dt, warn, critical)
        except DeviceAuthFailed as exc:
            self.auth_backoff.block(dev.id, password)
            return clockmod.failed_check(dev.id, cam_id, f"No se pudo leer la hora: {exc.message}")
        except VmsError as exc:
            return clockmod.failed_check(dev.id, cam_id, f"No se pudo leer la hora: {exc.message}")
        except Exception:  # noqa: BLE001
            log.exception("Error leyendo la hora de %s", dev.id)
            return clockmod.failed_check(dev.id, cam_id, "No se pudo leer la hora del equipo.")
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    log.debug("Error cerrando el cliente", exc_info=True)

    async def _clock_loop(self) -> None:
        await asyncio.sleep(self.initial_delay + 15)
        while True:
            try:
                await self.clock_check_all()
            except Exception:  # noqa: BLE001
                log.exception("Error comprobando la hora de los equipos")
            await asyncio.sleep(CLOCK_PERIOD_S)

    # ================================================================== previsión e informe
    def forecast(self, add_cameras: int = 0, bitrate_mbps: float = 0.0) -> RetentionForecast:
        cfg = self.state.config()
        cams = [c.id for c in cfg.cameras if c.enabled and c.record]
        return compute_forecast(self.state.recordings_dir(cfg), cams, cfg.settings.retention.days,
                                cfg.settings.retention.disk_guard_percent,
                                extra_bytes_per_hour=add_cameras * mbps_to_bytes_per_hour(bitrate_mbps))

    async def report(self, day: date | None = None) -> HealthReport:
        return (await self._report(day))[0]

    async def _report(self, day: date | None = None) \
            -> tuple[HealthReport, tuple[datetime, datetime, list[CameraInput]]]:
        cfg = self.state.config()
        site = cfg.settings.site
        tz = site_tz(site.timezone)
        now = datetime.now(timezone.utc)
        day = day or now.astimezone(tz).date()
        start, end = day_bounds(day, site.timezone)
        overview = await self.state.overview()
        online = {c["camera_id"]: c["online"] for c in overview["cameras"]}
        scores = await asyncio.to_thread(self.store.health_day_scores, start, end, cfg.settings.health.hysteresis)
        clocks = self.store.latest_clock_checks()
        fc = await asyncio.to_thread(self.forecast)
        days_on_disk = {c.camera_id: c.days_on_disk for c in fc.cameras}
        protected = self.bookmarks.protected_summary()
        inputs = []
        for cam in cfg.cameras:
            if not cam.enabled:
                continue
            try:
                spans = await self.state.engine.list_recordings(cam.id, start, end) if cam.record else []
            except (EngineUnavailable, VmsError):
                spans = None
            sc = scores.get(cam.id, (None, []))
            on = online.get(cam.id)
            inputs.append(CameraInput(camera_id=cam.id, name=cam.name, record=cam.record,
                                      online_now=None if on is None else bool(on), spans=spans, score_min=sc[0],
                                      causes=list(sc[1]), clock=clocks.get(cam.device_id),
                                      days_on_disk=days_on_disk.get(cam.id), protected=protected.get(cam.id, (0, None))[0]))
        disk: dict[str, float] = {}
        d = overview.get("disk")
        if d is not None:
            disk = {"percent": round(float(d.percent), 1), "free_gb": round(d.free / 1e9, 1)}
        disk.update(read_smart(self.ops_dir))
        oldest = [o for _, o in protected.values() if o is not None]
        oldest_days = (now - min(oldest)).total_seconds() / 86400 if oldest else None
        report = build(site.id, day, site.timezone, inputs, now=now, forecast=fc, disk=disk,
                       pc_clock=clocks.get(None), protected_oldest_days=oldest_days)
        return report, (start, min(end, now), inputs)

    async def refresh_summary(self) -> dict[str, Any]:
        report, inputs = await self._report()
        data = await asyncio.to_thread(write_summary, self.ops_dir, report)
        await self._recording_gap_alerts(*inputs)
        await self._disk_alerts(report.disk)
        return data

    async def _recording_gap_alerts(self, start: datetime, upto: datetime, cams: list[CameraInput]) -> None:
        """Aviso `recording_gap` si una cámara CON vídeo lleva `RECORDING_GAP_ALERT_MIN` sin grabar (hueco abierto
        al final del día en curso) y `recording_gap_cleared` cuando vuelve a grabar. Una cámara sin vídeo ya
        avisa como `camera_down`."""
        for c in cams:
            if not c.record or c.spans is None:
                continue
            open_gap = None
            gs = gaps(c.spans, start, upto)
            if gs and gs[-1][1] >= upto and (upto - gs[-1][0]).total_seconds() >= RECORDING_GAP_ALERT_MIN * 60:
                open_gap = gs[-1]
            if open_gap is not None and c.online_now and c.camera_id not in self._gap_alerted:
                self._gap_alerted.add(c.camera_id)
                since = open_gap[0].astimezone(site_tz(self.state.config().settings.site.timezone))
                await self.notifier.emit(Alert(kind="recording_gap", severity="warning",
                                               title_es=f"No graba desde las {since.strftime('%H:%M')}: {c.name}",
                                               camera_id=c.camera_id, camera_name=c.name,
                                               details={"since": open_gap[0].isoformat()}))
            elif open_gap is None and c.camera_id in self._gap_alerted:
                self._gap_alerted.discard(c.camera_id)
                await self.notifier.emit(Alert(kind="recording_gap_cleared", severity="info",
                                               title_es=f"Vuelve a grabar: {c.name}", camera_id=c.camera_id,
                                               camera_name=c.name))

    async def _disk_alerts(self, disk: dict[str, float]) -> None:
        """Avisos `disk` (como mucho uno al día por motivo): SMART con fallos, casi sin espacio o disco por encima
        del límite de la retención (se borran grabaciones antes de cumplir los días)."""
        today = datetime.now(timezone.utc).date()
        guard = self.state.config().settings.retention.disk_guard_percent
        checks: list[tuple[str, Severity, str]] = []
        if disk.get("smart_ok") == 0:
            checks.append(("smart", "critical", "El disco de grabación avisa de fallos (SMART): cámbialo pronto"))
        free_gb = disk.get("free_gb")
        if free_gb is not None and free_gb * 1e9 < DISK_LOW_FREE_BYTES:
            checks.append(("free", "critical", f"Quedan {free_gb:.1f} GB libres en el disco de grabación: "
                                               "la grabación puede pararse".replace(".", ",", 1)))
        percent = disk.get("percent")
        if percent is not None and guard and percent >= guard:
            checks.append(("guard", "warning", f"Disco de grabación al {percent:.0f} %: se borran grabaciones "
                                               "antiguas antes de cumplir los días de retención"))
        for reason, sev, title in checks:
            if self._disk_alerted.get(reason) == today:
                continue
            self._disk_alerted[reason] = today
            await self.notifier.emit(Alert(kind="disk", severity=sev, title_es=title, details={"reason": reason}))

    async def _monitor_loop(self) -> None:
        last_summary = 0.0
        await asyncio.sleep(min(30.0, self.initial_delay))
        while True:
            try:
                await self._check_down()
                if time.monotonic() - last_summary >= SUMMARY_PERIOD_S:
                    last_summary = time.monotonic()
                    await self.refresh_summary()
                    await self._forecast_alert()
            except Exception:  # noqa: BLE001
                log.exception("Error en la vigilancia de operación")
            await asyncio.sleep(MONITOR_PERIOD_S)

    async def _check_down(self) -> None:
        ov = await self.state.overview()
        if ov["status"] == "down":
            return
        names = {c.id: c.name for c in self.state.config().cameras}
        for c in ov["cameras"]:
            cid = c["camera_id"]
            if not c["enabled"] or c["online"] is None:
                continue
            if c["online"] is False:
                self._down[cid] = self._down.get(cid, 0) + 1
                if self._down[cid] == CAMERA_DOWN_POLLS and cid not in self._down_alerted:
                    self._down_alerted.add(cid)
                    await self.notifier.emit(Alert(kind="camera_down", severity="critical",
                                                   title_es=f"Cámara sin vídeo: {names.get(cid, cid)}",
                                                   camera_id=cid, camera_name=names.get(cid, cid),
                                                   details={"last_error": (c.get("last_error") or "")[:200]}))
            else:
                self._down.pop(cid, None)
                if cid in self._down_alerted:
                    self._down_alerted.discard(cid)
                    await self.notifier.emit(Alert(kind="camera_up", severity="info",
                                                   title_es=f"Cámara recuperada: {names.get(cid, cid)}",
                                                   camera_id=cid, camera_name=names.get(cid, cid)))

    async def _forecast_alert(self) -> None:
        fc = await asyncio.to_thread(self.forecast)
        today = datetime.now(timezone.utc).date()
        if fc.status != "ok" and self._last_forecast_alert != today:
            self._last_forecast_alert = today
            await self.notifier.emit(Alert(kind="retention_forecast", severity=fc.status,
                                           title_es=f"Solo caben unos {fc.forecast_days:.0f} días de grabación "
                                                    f"(objetivo {fc.target_days})",
                                           details={"forecast_days": fc.forecast_days, "target_days": fc.target_days}))

    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(self.initial_delay + 30)
        while True:
            try:
                await self.maintenance()
            except Exception:  # noqa: BLE001
                log.exception("Error en el mantenimiento de operación")
            await asyncio.sleep(MAINTENANCE_PERIOD_S)

    async def maintenance(self) -> None:
        released = await asyncio.to_thread(self.bookmarks.expire)
        for bm in released:
            audit("evidence_protect_expired", user="sistema", ip="", bookmark_id=bm.id, camera_id=bm.camera_id)
            self.state.bus.publish(EVENT_BOOKMARK, {"action": "updated", "bookmark_id": bm.id, "camera_id": bm.camera_id})
        for folder in await asyncio.to_thread(self.bookmarks.sweep_orphans):
            log.info("Borrada la copia protegida pendiente %s", folder)
        await asyncio.to_thread(self.expire_exports)
        await asyncio.to_thread(self.store.prune)
        known = {c.id for c in self.state.config().cameras}
        for cid in self.refs.camera_ids() - known:   # RGPD: referencias de cámaras borradas
            self.refs.delete_camera(cid)
            self.tracker.forget(cid)
            self.store.kv_set(f"health_state:{cid}", None)

    # ================================================================== línea de tiempo
    async def timeline(self, camera_id: str, start: datetime, end: datetime, layers: set[str]) -> list[TimelineEvent]:
        out: list[TimelineEvent] = []
        if "recording_gap" in layers:
            try:
                spans = await self.state.engine.list_recordings(camera_id, start, end)
                upto = min(end, datetime.now(timezone.utc))
                for a, b in gaps(spans, start, upto):
                    out.append(TimelineEvent(camera_id=camera_id, layer="recording_gap", start=a, end=b,
                                             severity="warning", title_es="Sin grabación"))
            except (EngineUnavailable, VmsError):
                pass
        if layers & {"bookmark", "protected"}:
            for bm in await asyncio.to_thread(self.store.bookmarks, camera_id, start, end):
                if "bookmark" in layers:
                    out.append(TimelineEvent(camera_id=camera_id, layer="bookmark", start=bm.start, end=bm.end,
                                             title_es=bm.note or "Marcador", ref_id=bm.id))
                if "protected" in layers and bm.protected:
                    out.append(TimelineEvent(camera_id=camera_id, layer="protected", start=bm.start,
                                             end=bm.end or bm.start + timedelta(minutes=1),
                                             title_es=f"Protegido hasta {_utc(bm.protect_until).date().isoformat()}"
                                             if bm.protect_until else "Protegido", ref_id=bm.id))
        stored = layers & {"health", "clock"}
        if stored:
            out += await asyncio.to_thread(self.store.timeline_events, camera_id, start, end, stored)
        if "analytics_alert" in layers:
            out += await self._analytics_alerts(camera_id, start, end)
        return sorted(out, key=lambda e: e.start)

    async def _analytics_alerts(self, camera_id: str, start: datetime, end: datetime) -> list[TimelineEvent]:
        dsn = self.state.settings.pg_dsn
        if not dsn:
            return []
        try:
            import psycopg
            async with await psycopg.AsyncConnection.connect(dsn.get_secret_value(), connect_timeout=3) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT started_at, ended_at, peak_people, threshold FROM queue_alerts WHERE site_id = %s "
                        "AND camera_id = %s AND started_at < %s AND coalesce(ended_at, now()) >= %s ORDER BY started_at",
                        (self.state.site().id, camera_id, end, start))
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001 - la central puede no estar accesible
            log.info("Alertas de analítica no disponibles para la línea de tiempo: %s", type(exc).__name__)
            return []
        return [TimelineEvent(camera_id=camera_id, layer="analytics_alert", start=r[0], end=r[1], severity="warning",
                              title_es=f"Cola: {r[2]} personas (umbral {r[3]})") for r in rows]

    # ================================================================== evidencias
    def key(self) -> EvidenceKey:
        if self._key is None:
            self._key = load_or_create(self.state.paths)
        return self._key

    def start_export(self, req: EvidenceExportRequest, user: str, ip: str) -> EvidenceExport:
        cfg = self.state.config()
        for cid in req.camera_ids:
            if cfg.camera(cid) is None:
                raise NotFoundError("La cámara no existe")
        exp = EvidenceExport(export_id=new_export_id(), created_by=user, request=req)
        self.store.save_export(exp)
        self._evidence_event(exp, "queued", 0.0, "En cola")
        self._spawn(self._run_export(exp, ip), f"ops-export-{exp.export_id}")
        return exp

    async def _fetch_mp4(self, camera_id: str, start: datetime, duration: float) -> AsyncIterator[bytes]:
        proxy = self.state.proxy
        if proxy is None:
            raise RuntimeError("el servidor de reproducción no está listo")
        url = self.state.engine.playback_get_url(camera_id, start, duration, "mp4")
        import httpx
        req = proxy.build_request("GET", url, timeout=httpx.Timeout(30.0, read=120.0))
        resp = await proxy.send(req, stream=True)
        try:
            if resp.status_code != 200:
                raise RuntimeError(f"el servidor de reproducción respondió {resp.status_code}")
            async for chunk in resp.aiter_bytes(256 * 1024):
                yield chunk
        finally:
            await resp.aclose()

    def _export_context(self, req: EvidenceExportRequest, paths_status: dict[str, Any] | None) -> ExportContext:
        cfg = self.state.config()
        site = cfg.settings.site
        clocks = self.store.latest_clock_checks()
        cams: dict[str, CameraInfo] = {}
        for cid in req.camera_ids:
            cam = cfg.camera(cid)
            if cam is None:
                continue
            dev = cfg.device(cam.device_id)
            st = (paths_status or {}).get(f"{cid}/main")
            codec = ", ".join(t for t in (st.tracks if st else []) if t.upper().startswith(("H26", "HEVC", "MJPEG")))
            clk = clocks.get(cam.device_id)
            cams[cid] = CameraInfo(camera_id=cid, name=cam.name, device=dev.name if dev else "", codec=codec,
                                   clock_skew_s=clk.skew_s if clk else None,
                                   clock_measured_at=clk.at.isoformat() if clk and clk.skew_s is not None else None)
        pc = clocks.get(None)
        return ExportContext(
            product_version=__version__,
            site={"id": site.id, "name": site.name, "code": site.code, "timezone": site.timezone},
            cameras=cams, recordings_dir=Path(self.state.recordings_dir(cfg)), protected_dir=self.protected_dir,
            segment_seconds=cfg.settings.recording.segment_seconds, key=self.key(),
            fetch_mp4=self._fetch_mp4 if self.state.proxy is not None else None,
            pc_clock=pc.model_dump(mode="json") if pc else {})

    async def _run_export(self, exp: EvidenceExport, ip: str) -> None:
        last = 0.0

        async def progress(p: float, msg: str) -> None:
            nonlocal last
            exp.progress = p
            if p - last >= 0.05 or p >= 1.0:
                last = p
                await asyncio.to_thread(self.store.save_export, exp)
                self._evidence_event(exp, "running", p, msg)

        exp.state = "running"
        await asyncio.to_thread(self.store.save_export, exp)
        try:
            ctx = await asyncio.to_thread(self._export_context, exp.request, await self.state.paths_status_safe())
            path, manifest_sha, count = await self.builder.build(exp, ctx, progress)
            exp.state, exp.progress = "done", 1.0
            exp.bytes = path.stat().st_size
            exp.download_url = f"/api/evidence/exports/{exp.export_id}/download"
            exp.sha256_manifest, exp.files = manifest_sha, count
            exp.finished_at = datetime.now(timezone.utc)
            req = exp.request
            audit("evidence_export", user=exp.created_by, ip=ip, cameras=req.camera_ids,
                  **{"from": _utc(req.start).isoformat(), "to": _utc(req.end).isoformat()}, reason=req.reason,
                  case_ref=req.case_ref, export_id=exp.export_id, sha256_manifest=manifest_sha)
            msg = "Paquete listo para descargar"
        except ValueError as exc:
            exp.state, exp.error, msg = "failed", str(exc), str(exc)
        except Exception:  # noqa: BLE001
            log.exception("Error generando la exportación %s", exp.export_id)
            exp.state, exp.error = "failed", "No se pudo generar el paquete; revisa el registro del sistema"
            msg = exp.error
        await asyncio.to_thread(self.store.save_export, exp)
        self._evidence_event(exp, exp.state, exp.progress, msg)

    def _evidence_event(self, exp: EvidenceExport, state: str, progress: float, msg: str) -> None:
        # `_owner` y `_cameras` no salen al navegador: /api/events los usa para mandar el progreso solo a quien
        # pidió la exportación (o a un administrador) mientras siga pudiendo exportar esas cámaras
        self.state.bus.publish(EVENT_EVIDENCE, {"export_id": exp.export_id, "state": state, "progress": progress,
                                                "message_es": msg, "_owner": exp.created_by,
                                                "_cameras": list(exp.request.camera_ids)})

    def delete_export(self, export_id: str) -> None:
        self.builder.zip_path(export_id).unlink(missing_ok=True)
        self.builder.part_path(export_id).unlink(missing_ok=True)
        self.store.delete_export(export_id)

    def pc_sntp_enabled(self) -> bool:
        v = self.store.kv_get(PC_SNTP_KEY)
        return v if isinstance(v, bool) else True

    def set_pc_sntp_enabled(self, enabled: bool) -> None:
        self.store.kv_set(PC_SNTP_KEY, bool(enabled))

    def export_retention_days(self) -> int:
        v = self.store.kv_get(EXPORT_RETENTION_KEY)
        return int(v) if isinstance(v, int) and 1 <= v <= 365 else EXPORT_RETENTION_DEFAULT_DAYS

    def set_export_retention_days(self, days: int) -> None:
        if not 1 <= int(days) <= 365:
            raise ValueError("export_retention_days fuera de rango")
        self.store.kv_set(EXPORT_RETENTION_KEY, int(days))

    def expire_exports(self, now: datetime | None = None) -> list[str]:
        """Borra los paquetes (ZIP y registro) con más de `export_retention_days` días: son copias de vídeo con
        personas y no pueden quedarse en el PC sin plazo (RGPD, EIPD §2.6). Queda anotado en audit.log."""
        now = now or datetime.now(timezone.utc)
        days = self.export_retention_days()
        removed = []
        for exp in self.store.exports_created_before(now - timedelta(days=days)):
            if exp.state in ("queued", "running"):
                continue
            try:
                self.delete_export(exp.export_id)
            except OSError as exc:
                log.warning("No se pudo borrar la exportación caducada %s: %s", exp.export_id, exc)
                continue
            audit("evidence_export_expired", user="sistema", ip="", export_id=exp.export_id,
                  created_by=exp.created_by, sha256_manifest=exp.sha256_manifest, retention_days=days)
            removed.append(exp.export_id)
        return removed

    def recover_exports(self) -> list[str]:
        """Al arrancar: las exportaciones «en cola» o «en curso» se cortaron con el proceso; se dan por fallidas y
        se borran sus restos (`*.zip.part`, `.<id>.tmp`) para que no quede vídeo copiado sin control."""
        failed = []
        for exp in self.store.exports_in_state("queued", "running"):
            exp.state = "failed"
            exp.error = "Se interrumpió porque el servicio se reinició: vuelve a pedirla"
            self.store.save_export(exp)
            failed.append(exp.export_id)
        leftovers = self.builder.cleanup_leftovers()
        if failed or leftovers:
            log.warning("Exportaciones interrumpidas: %s; restos borrados: %s", failed or "-", leftovers or "-")
        return failed

    # ================================================================== diagnóstico y auditoría
    def diagnoser(self) -> Diagnoser:
        hs = self.state.config().settings.health

        async def engine_running() -> bool:
            try:
                return bool((await self.state.engine.status()).running)
            except Exception:  # noqa: BLE001
                return False

        return Diagnoser(self.state.client_factory, engine_running=engine_running,
                         paths_status=self.state.paths_status_safe,
                         clock_thresholds=(hs.clock_warn_s, hs.clock_critical_s),
                         rewriter=llm_rewriter_from_settings(self.state.settings))

    async def diagnose(self, inp: DiagnoseInput) -> DiagnosisResult:
        return await self.diagnoser().run(inp)

    async def security_audit(self, admin_credentials: dict[str, tuple[str, str]], user: str, ip: str,
                             deps: AuditDeps | None = None) -> SecurityAuditReport:
        cfg = self.state.config()
        table = load_table(self.state.paths.base)
        deps = deps or AuditDeps(client_factory=self.state.client_factory,
                                 get_password=self.state.creds.get_device_password)
        report = await run_audit(cfg.settings.site.id, list(cfg.devices), list(cfg.cameras), deps, table,
                                 self.store.latest_clock_checks(), admin_credentials)
        self.store.kv_set("security_audit_latest", report.model_dump(mode="json"))
        audit("security_audit", user=user, ip=ip, devices=report.devices_checked,
              admin_credentials_used=report.admin_credentials_used,
              findings_bad=sum(1 for f in report.findings if f.status in ("vulnerable", "probably_vulnerable")))
        return report

    def latest_audit(self) -> SecurityAuditReport | None:
        data = self.store.kv_get("security_audit_latest")
        return SecurityAuditReport.model_validate(data) if isinstance(data, dict) else None
