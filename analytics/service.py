"""Servicio de analítica de la sede (`python -m analytics`).

Orquesta todo lo demás en un bucle asíncrono:
- cada 30 s pide la configuración al backend (con caché local si no responde);
- arranca, reinicia o para el procesado de cada cámara según esa configuración;
- cada segundo cierra los minutos terminados y los envía a PostgreSQL (con cola en disco);
- recibe las alertas de cola, las guarda y avisa por Telegram sin bloquear el conteo;
- cada 10 s escribe `<datos>/analytics/status.json` (fps, ms de inferencia, errores) que el
  panel muestra en «Estado».
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vms import __version__
from vms.core.atomic import atomic_write_text
from vms.core.models import LineRule
from vms.core.mtx_auth import MtxCredentials, with_reader_credentials
from vms.core.settings import VmsSettings

from .aggregation import MinuteAggregator, Record
from .alerts import AlertEvent
from .config import AnalyticsConfig, CameraJob, ConfigSource
from .detector import DetectorRegistry
from .pipeline import CameraPipeline
from .settings import AnalyticsSettings, resolve_backend_url, resolve_models_dir
from .storage import Persistence, PgStore, Spool
from .telegram import TelegramError, TelegramNotifier, alert_ended_text, alert_started_text
from .video import FrameGrabber

log = logging.getLogger("analytics.service")

OPEN_MINUTES_FILE = "open-minutes.json"


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def alert_record(ev: AlertEvent, *, notified_at: datetime | None = None, notify_error: str | None = None) -> Record:
    return {"type": "alert", "alert_id": ev.alert_id, "rule_id": ev.rule_id, "camera_id": ev.camera_id,
            "started_at": _iso(ev.started_at), "ended_at": _iso(ev.ended_at), "peak_people": ev.peak_people,
            "threshold": ev.threshold, "notified_at": _iso(notified_at), "notify_error": notify_error}


def meta_records(cfg: AnalyticsConfig) -> list[Record]:
    """Sede, cámaras y reglas, para que el panel central y el informe sepan qué es cada número."""
    out: list[Record] = [{"type": "site", "name": cfg.site.name, "code": cfg.site.code, "timezone": cfg.site.timezone}]
    ids: list[str] = []
    for cam in cfg.cameras:
        out.append({"type": "camera", "camera_id": cam.camera_id, "name": cam.name or cam.camera_id})
        for r in cam.rules:
            conf = r.model_dump(mode="json", exclude={"id", "camera_id", "name", "enabled", "updated_at", "kind"})
            out.append({"type": "rule", "rule_id": r.id, "camera_id": r.camera_id, "kind": r.kind, "name": r.name,
                        "config": conf, "active": r.enabled})
            if r.enabled:
                ids.append(r.id)
    out.append({"type": "rules_active", "rule_ids": ids})
    return out


class AnalyticsService:
    def __init__(self, settings: VmsSettings, asettings: AnalyticsSettings, *,
                 static_config: AnalyticsConfig | None = None,
                 config_source: ConfigSource | None = None,
                 registry: DetectorRegistry | None = None,
                 notifier: TelegramNotifier | None = None,
                 store: PgStore | None = None,
                 grabber_factory: Callable[[str, str], FrameGrabber] | None = None) -> None:
        self.settings = settings
        self.asettings = asettings
        self.paths = settings.paths.ensure()
        self.dir = self.paths.analytics_dir
        self._static = static_config
        self._source = config_source
        self.registry = registry or DetectorRegistry(
            resolve_models_dir(settings, asettings), asettings.inference_backend,
            threads=asettings.inference_threads, openvino_precision=asettings.openvino_precision)
        token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
        self.notifier = notifier or (TelegramNotifier(token, api_base=asettings.telegram_api_base) if token else None)
        self._owns_notifier = notifier is None
        self._store_override = store
        # La analítica lee del MediaMTX local con el usuario interno de solo lectura (vms.core.mtx_auth);
        # la URL de la configuración no lleva credenciales (se guarda en disco como caché).
        self._mtx_creds = MtxCredentials.from_internal_token(settings.ensure_internal_token())
        base_factory = grabber_factory or (lambda url, name: FrameGrabber(url, name))
        self._grabber_factory: Callable[[str, str], FrameGrabber] = lambda url, name: base_factory(
            with_reader_credentials(url, self._mtx_creds, settings.mtx_rtsp_address), name)
        self.aggregator = MinuteAggregator()
        self.persistence: Persistence | None = None
        self.config: AnalyticsConfig | None = None
        self.pipelines: dict[str, CameraPipeline] = {}
        self._alert_queue: asyncio.Queue[AlertEvent] = asyncio.Queue()
        self._tasks: list[asyncio.Task[Any]] = []
        self._notify_tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at = time.time()
        self._last_drain_fail = 0.0
        self._warned_no_telegram = False
        self.alerts_sent = 0

    # ================================================================== arranque / parada
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        spool = Spool(self.dir / "spool")
        store = self._store_override
        if store is None and self.settings.pg_dsn is not None:
            store = PgStore(self.settings.pg_dsn.get_secret_value(), self.settings.site_id)
        if store is None:
            log.warning("VMS_PG_DSN no está definido: los conteos se guardan en la cola local hasta configurarlo")
        self.persistence = Persistence(spool, store)
        self._restore_open_minutes()
        if self._source is None and self._static is None:
            token = self.settings.ensure_internal_token()
            self._source = ConfigSource(resolve_backend_url(self.settings, self.asettings), token,
                                        self.dir / "config-cache.json")
        if self._static is not None:
            await self._apply(self._static)
        else:
            assert self._source is not None
            cfg = await self._source.fetch()
            if cfg is not None:
                await self._apply(cfg)
            elif self._source.current is None:
                log.warning("Aún no hay configuración de analítica (backend sin responder y sin caché)")
        self._tasks = [asyncio.create_task(self._flush_loop(), name="flush"),
                       asyncio.create_task(self._alert_loop(), name="alerts"),
                       asyncio.create_task(self._status_loop(), name="status")]
        if self._static is None:
            self._tasks.append(asyncio.create_task(self._config_loop(), name="config"))
        await self._write_status()

    async def stop(self, *, flush_open_minutes: bool = False) -> None:
        """Para todo de forma ordenada.

        flush_open_minutes=False (servicio normal): el minuto en curso se guarda en disco y se
        recupera al volver a arrancar. True (pruebas, ejecución puntual): se escribe ya en la base.
        """
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for cid in list(self.pipelines):
            await self._stop_pipeline(cid)
        await self._drain_alert_queue()
        if self._notify_tasks:
            await asyncio.wait(self._notify_tasks, timeout=15)
        assert self.persistence is not None
        if flush_open_minutes:
            self.persistence.submit(self.aggregator.pop_all())
        else:
            self.persistence.submit(self.aggregator.pop_closed(time.time(), self.asettings.minute_close_margin_seconds))
            self._save_open_minutes()
        try:
            await asyncio.wait_for(self.persistence.drain(max_files=10_000), timeout=20)
        except TimeoutError:
            log.warning("No dio tiempo a vaciar la cola hacia PostgreSQL; se reenviará al arrancar")
        if self.persistence.store is not None:
            await self.persistence.store.close()
        if self._source is not None:
            await self._source.aclose()
        if self.notifier is not None and self._owns_notifier:
            await self.notifier.aclose()
        await self._write_status(running=False)
        log.info("Analítica detenida")

    async def run(self, stop_event: asyncio.Event, *, max_seconds: float | None = None) -> None:
        await self.start()
        try:
            if max_seconds is None:
                await stop_event.wait()
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=max_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.stop(flush_open_minutes=max_seconds is not None)

    # ================================================================== configuración
    async def _config_loop(self) -> None:
        assert self._source is not None
        while True:
            await asyncio.sleep(self.asettings.config_poll_seconds)
            try:
                cfg = await self._source.fetch()
                if cfg is not None:
                    await self._apply(cfg)
            except Exception:  # no debe tumbar el servicio
                log.exception("Error al aplicar la configuración nueva")

    async def _apply(self, cfg: AnalyticsConfig) -> None:
        old = self.config
        if cfg.site.id != self.settings.site_id:
            log.warning("La configuración es de la sede %s pero VMS_SITE_ID=%s: se usa VMS_SITE_ID",
                        cfg.site.id, self.settings.site_id)
        self.config = cfg
        if self.persistence is not None and self.persistence.store is not None:
            self.persistence.store.site_name = cfg.site.name
        if old is None or old.revision != cfg.revision or old.model_dump() != cfg.model_dump():
            assert self.persistence is not None
            self.persistence.submit(meta_records(cfg))
            log.info("Configuración de analítica aplicada (revisión %s, %d cámaras)", cfg.revision, len(cfg.cameras))
        wanted = {c.camera_id: c for c in cfg.cameras}
        for cid in [c for c in self.pipelines if c not in wanted]:
            await self._stop_pipeline(cid)
        for cid, job in wanted.items():
            current = self.pipelines.get(cid)
            if current is None:
                self._start_pipeline(job)
            elif current.job.pipeline_key() != job.pipeline_key():
                await self._stop_pipeline(cid)
                self._start_pipeline(job)
            else:
                current.job = job
                current.set_rules(job.rules)

    def _start_pipeline(self, job: CameraJob) -> None:
        self.pipelines[job.camera_id] = CameraPipeline(job, self.registry, self.aggregator, self.asettings,
                                                       self._enqueue_alert_threadsafe,
                                                       grabber_factory=self._grabber_factory).start()

    async def _stop_pipeline(self, camera_id: str) -> None:
        p = self.pipelines.pop(camera_id, None)
        if p is None:
            return
        closed = await asyncio.to_thread(p.stop)
        for ev in closed:
            self._alert_queue.put_nowait(ev)

    # ================================================================== minutos → base de datos
    async def _flush_loop(self) -> None:
        assert self.persistence is not None
        while True:
            await asyncio.sleep(1.0)
            try:
                closed = self.aggregator.pop_closed(time.time(), self.asettings.minute_close_margin_seconds)
                if closed:
                    self.persistence.submit(closed)
                    log.debug("Minutos cerrados: %d registros", len(closed))
                await self._maybe_drain()
            except Exception:
                log.exception("Error en el envío de conteos")

    async def _maybe_drain(self) -> None:
        assert self.persistence is not None
        # Primero la espera entre reintentos (barata) y después cuántos lotes hay (índice en memoria).
        if not self.persistence.ok and time.monotonic() - self._last_drain_fail < self.asettings.db_retry_seconds:
            return
        if not self.persistence.has_pending():
            return
        await self.persistence.drain()
        if not self.persistence.ok:
            self._last_drain_fail = time.monotonic()

    # ================================================================== alertas
    def _enqueue_alert_threadsafe(self, ev: AlertEvent) -> None:
        """Lo llaman los hilos de cámara: pasa el evento al bucle asíncrono."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._alert_queue.put_nowait, ev)

    async def _alert_loop(self) -> None:
        while True:
            ev = await self._alert_queue.get()
            try:
                self._handle_alert(ev)
            except Exception:
                log.exception("Error al registrar una alerta de cola")

    async def _drain_alert_queue(self) -> None:
        while not self._alert_queue.empty():
            self._handle_alert(self._alert_queue.get_nowait())

    def _handle_alert(self, ev: AlertEvent) -> None:
        assert self.persistence is not None
        cam = self.config.camera(ev.camera_id) if self.config else None
        cam_name = (cam.name if cam else "") or ev.camera_id
        if ev.kind == "started":
            log.warning("ALERTA de cola en «%s» (%s): %d personas (umbral %d)", ev.rule_name, cam_name,
                        ev.occupancy, ev.threshold)
        else:
            log.info("Fin de la alerta de cola en «%s» (%s): duró %.0f s, máximo %d", ev.rule_name, cam_name,
                     ev.duration_seconds, ev.peak_people)
        try:
            self.persistence.submit([alert_record(ev)])
        except Exception:  # el aviso por Telegram sale aunque no se pueda guardar la alerta
            log.exception("No se pudo guardar la alerta de cola; se avisa igualmente")
        chat = self._telegram_chat()
        if chat is None:
            return
        if ev.kind == "ended" and not self.asettings.notify_clear:
            return
        task = asyncio.create_task(self._notify(ev, chat, cam_name))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _telegram_chat(self) -> str | None:
        cfg = self.config
        if cfg is None or not cfg.alerts.telegram_enabled:
            return None
        if not cfg.alerts.telegram_chat_id or self.notifier is None:
            if not self._warned_no_telegram:
                log.warning("Telegram está activado pero falta %s: la alerta solo se guarda en la base",
                            "el token (VMS_TELEGRAM_BOT_TOKEN)" if self.notifier is None else "el ID del chat")
                self._warned_no_telegram = True
            return None
        return cfg.alerts.telegram_chat_id

    async def _notify(self, ev: AlertEvent, chat: str, cam_name: str) -> None:
        assert self.notifier is not None and self.persistence is not None and self.config is not None
        site = self.config.site
        text = (alert_started_text(ev, site.name, cam_name, site.timezone) if ev.kind == "started"
                else alert_ended_text(ev, site.name, cam_name, site.timezone))
        try:
            await self.notifier.send(chat, text)
        except TelegramError as exc:
            log.error("No se pudo avisar por Telegram: %s", exc)
            if ev.kind == "started":
                self.persistence.submit([alert_record(ev, notify_error=str(exc)[:500])])
            return
        except Exception as exc:
            log.exception("Error inesperado al avisar por Telegram")
            if ev.kind == "started":
                self.persistence.submit([alert_record(ev, notify_error=f"{type(exc).__name__}: {exc}"[:500])])
            return
        self.alerts_sent += 1
        if ev.kind == "started":
            self.persistence.submit([alert_record(ev, notified_at=datetime.now(timezone.utc))])

    # ================================================================== minuto en curso entre reinicios
    def _save_open_minutes(self) -> None:
        data = self.aggregator.export_open()
        if not data:
            return
        try:
            atomic_write_text(self.dir / OPEN_MINUTES_FILE, json.dumps(data))
            log.info("Minuto en curso guardado para el próximo arranque (%d registros)", len(data))
        except OSError as exc:
            log.error("No se pudo guardar el minuto en curso: %s", exc)

    def _restore_open_minutes(self) -> None:
        path = self.dir / OPEN_MINUTES_FILE
        if not path.is_file():
            return
        try:
            n = self.aggregator.import_open(json.loads(path.read_text(encoding="utf-8")))
            log.info("Recuperados %d minutos a medias del arranque anterior", n)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.error("No se pudo recuperar el minuto en curso anterior: %s", exc)
        try:
            path.unlink()
        except OSError as exc:
            log.warning("No se pudo borrar %s: %s", path.name, exc)

    # ================================================================== estado
    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(self.asettings.status_interval_seconds)
            try:
                await self._write_status()
            except Exception:
                log.exception("No se pudo escribir status.json")

    def status(self, running: bool = True) -> dict[str, Any]:
        p = self.persistence
        cams = [pl.status() for pl in self.pipelines.values()]
        return {
            "running": running,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pid": os.getpid(),
            "version": __version__,
            "uptime_s": round(time.time() - self._started_at),
            "config_revision": self.config.revision if self.config else None,
            "config_error": self._source.last_error if self._source else "",
            "db": {"ok": bool(p and p.ok), "spool_pending": p.spool.pending() if p else 0,
                   "memory_pending": p.memory_pending if p else 0,
                   "last_error": p.last_error if p else "",
                   "disk_error": p.disk_error if p else ""},
            "telegram": {"enabled": bool(self.config and self.config.alerts.telegram_enabled),
                         "token_configured": self.notifier is not None, "sent": self.alerts_sent},
            "cameras": cams,
        }

    async def _write_status(self, running: bool = True) -> None:
        st = self.status(running)
        for c in st["cameras"]:
            if c["state"] == "running" and c["inference_ms_p50"] is not None:
                log.debug("[%s] %.1f fps procesados (entrada %.1f), inferencia p50 %.1f ms", c["camera_id"],
                          c["fps_processed"], c["fps_in"], c["inference_ms_p50"])
        await asyncio.to_thread(atomic_write_text, self.dir / "status.json",
                                json.dumps(st, ensure_ascii=False, indent=1))


# =========================================================================== instancia única
class SingleInstanceLock:
    """Evita dos procesos de analítica a la vez en la misma carpeta de datos (contarían doble)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            log.debug("Error al liberar el bloqueo: %s", exc)
        self._fh.close()
        self._fh = None


def summarize_rules(cfg: AnalyticsConfig) -> str:
    parts = []
    for c in cfg.cameras:
        n_line = sum(isinstance(r, LineRule) for r in c.rules)
        parts.append(f"{c.name or c.camera_id}: {n_line} líneas, {len(c.rules) - n_line} zonas, {c.fps} fps")
    return "; ".join(parts) or "sin cámaras"
