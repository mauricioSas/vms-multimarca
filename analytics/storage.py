"""Persistencia de los conteos en PostgreSQL, con cola local en disco si la base no responde.

Cómo se garantiza que no se pierde ningún minuto cerrado:
1. Cada lote de registros (minutos cerrados, alertas, cambios de reglas) se escribe PRIMERO en
   un archivo de la cola local `<datos>/analytics/spool/` («write-ahead»).
2. Después se intenta enviar a PostgreSQL, archivo a archivo y en orden. Cuando un archivo entra
   en la base (en una transacción), se borra.
3. Si PostgreSQL no responde (corte de VPN, mantenimiento...), los archivos esperan y se
   reenvían en el mismo orden al volver. Las escrituras son idempotentes (upsert), así que
   reenviar un lote que ya había entrado no duplica nada.

Solo se guardan números y nombres de reglas/cámaras: nunca imágenes (RGPD).
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Iterable
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from vms.core.atomic import atomic_write_text
from vms.core.rtsp import redact

from .aggregation import Record

log = logging.getLogger("analytics.storage")

MAX_SPOOL_FILES = 50_000   # ~1 mes de lotes por minuto; por encima se descarta lo más antiguo
MAX_MEMORY_BATCHES = 2_000  # lotes en memoria si el disco no deja escribir (~1 día y medio de minutos)


class StoreError(Exception):
    """PostgreSQL no disponible o rechazó el lote (mensaje sin credenciales)."""


# =========================================================================== cola en disco
class Spool:
    """Cola de lotes en disco, en orden de llegada.

    El índice de archivos vive en memoria (se carga una vez, en el primer uso): con PostgreSQL
    caído varios días hay decenas de miles de lotes y listar la carpeta en cada operación
    bloqueaba el bucle asíncrono casi medio segundo por segundo.
    """

    def __init__(self, directory: Path, max_files: int = MAX_SPOOL_FILES) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self._seq = 0
        self._index: deque[Path] | None = None

    def _files(self) -> deque[Path]:
        if self._index is None:
            self._index = deque(sorted(p for p in self.dir.glob("*.jsonl") if p.is_file()))
        return self._index

    def rescan(self) -> None:
        """Vuelve a leer la carpeta (por si alguien añadió o quitó archivos a mano)."""
        self._index = None
        self._files()

    def append(self, records: list[Record]) -> Path | None:
        """Escribe un lote. Lanza OSError si el disco está lleno o no se puede escribir."""
        if not records:
            return None
        index = self._files()  # se carga ANTES de crear el archivo (si no, aparecería dos veces)
        self._seq += 1
        name = f"{time.time_ns():020d}-{os.getpid()}-{self._seq:06d}.jsonl"
        path = self.dir / name
        atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        index.append(path)
        self._enforce_limit()
        return path

    def files(self) -> list[Path]:
        return list(self._files())

    def head(self, n: int) -> list[Path]:
        """Los `n` lotes más antiguos, en orden."""
        return list(islice(self._files(), max(0, n)))

    def pending(self) -> int:
        return len(self._files())

    def read(self, path: Path) -> list[Record]:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _forget(self, path: Path) -> None:
        idx = self._files()
        if idx and idx[0] == path:
            idx.popleft()
        else:
            try:
                idx.remove(path)
            except ValueError:
                pass

    def remove(self, path: Path) -> None:
        self._forget(path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def quarantine(self, path: Path, reason: str) -> None:
        self._forget(path)
        bad = self.dir / "bad"
        log.error("Lote de la cola ilegible o rechazado de forma permanente (%s): se aparta en %s", reason, bad)
        try:
            bad.mkdir(exist_ok=True)
            path.replace(bad / path.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("No se pudo apartar %s: %s", path.name, exc)

    def _enforce_limit(self) -> None:
        idx = self._files()
        excess = len(idx) - self.max_files
        if excess > 0:
            log.error("La cola local supera %d lotes (PostgreSQL lleva mucho sin responder): "
                      "se descartan los %d más antiguos", self.max_files, excess)
            for _ in range(excess):
                self.remove(idx[0])


# =========================================================================== PostgreSQL
_SQL_SITE_ENSURE = ("INSERT INTO sites (site_id, name) VALUES (%s, %s) ON CONFLICT (site_id) DO NOTHING")
_SQL_SITE = """
INSERT INTO sites (site_id, name, code, timezone, updated_at) VALUES (%s, %s, %s, %s, now())
ON CONFLICT (site_id) DO UPDATE SET name = EXCLUDED.name, code = EXCLUDED.code,
    timezone = EXCLUDED.timezone, updated_at = now()"""
_SQL_CAMERA = """
INSERT INTO site_cameras (site_id, camera_id, name, updated_at) VALUES (%s, %s, %s, now())
ON CONFLICT (site_id, camera_id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()"""
_SQL_RULE = """
INSERT INTO analytics_rules (site_id, rule_id, camera_id, kind, name, config, active, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (site_id, rule_id) DO UPDATE SET camera_id = EXCLUDED.camera_id, kind = EXCLUDED.kind,
    name = EXCLUDED.name, config = EXCLUDED.config, active = EXCLUDED.active, updated_at = now()"""
_SQL_RULES_ACTIVE = "UPDATE analytics_rules SET active = (rule_id = ANY(%s)), updated_at = now() WHERE site_id = %s"
_SQL_LINE = """
INSERT INTO line_counts_minute (site_id, rule_id, camera_id, minute, count_in, count_out)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (site_id, rule_id, minute) DO UPDATE SET camera_id = EXCLUDED.camera_id,
    count_in = EXCLUDED.count_in, count_out = EXCLUDED.count_out"""
_SQL_ZONE = """
INSERT INTO zone_occupancy_minute (site_id, rule_id, camera_id, minute, samples, avg_people, max_people,
    seconds_over_threshold)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (site_id, rule_id, minute) DO UPDATE SET camera_id = EXCLUDED.camera_id,
    samples = EXCLUDED.samples, avg_people = EXCLUDED.avg_people, max_people = EXCLUDED.max_people,
    seconds_over_threshold = EXCLUDED.seconds_over_threshold"""
_SQL_ALERT = """
INSERT INTO queue_alerts (alert_id, site_id, rule_id, camera_id, started_at, ended_at, peak_people, threshold,
    notified_at, notify_error)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (alert_id) DO UPDATE SET
    ended_at = COALESCE(EXCLUDED.ended_at, queue_alerts.ended_at),
    peak_people = GREATEST(queue_alerts.peak_people, EXCLUDED.peak_people),
    notified_at = COALESCE(EXCLUDED.notified_at, queue_alerts.notified_at),
    notify_error = CASE WHEN COALESCE(EXCLUDED.notified_at, queue_alerts.notified_at) IS NOT NULL THEN NULL
                        ELSE COALESCE(EXCLUDED.notify_error, queue_alerts.notify_error) END"""


def _ts(value: Any) -> datetime | None:
    return None if value in (None, "") else datetime.fromisoformat(str(value))


class PgStore:
    """Escritura en PostgreSQL con una conexión que se reabre sola si se cae."""

    def __init__(self, dsn: str, site_id: str, site_name: str = "", *, connect_timeout: int = 5) -> None:
        self._dsn = dsn
        self.site_id = site_id
        self.site_name = site_name or site_id
        self._timeout = connect_timeout
        self._conn: psycopg.AsyncConnection[Any] | None = None

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except psycopg.Error as exc:
                log.debug("Error al cerrar la conexión: %s", redact(str(exc)))
            self._conn = None

    async def _connection(self) -> psycopg.AsyncConnection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(
                self._dsn, connect_timeout=self._timeout, application_name="vms-analytics")
        return self._conn

    async def write(self, records: Iterable[Record]) -> None:
        """Escribe un lote en UNA transacción (todo o nada). Lanza StoreError si falla."""
        groups: dict[str, list[tuple[Any, ...]]] = {k: [] for k in
                                                    ("site", "camera", "rule", "rules_active", "line", "zone", "alert")}
        sid = self.site_id
        for r in records:
            t = r.get("type")
            if t == "site":
                groups["site"].append((sid, r["name"], r.get("code", ""), r.get("timezone", "Europe/Madrid")))
            elif t == "camera":
                groups["camera"].append((sid, r["camera_id"], r["name"]))
            elif t == "rule":
                groups["rule"].append((sid, r["rule_id"], r["camera_id"], r["kind"], r["name"],
                                       Jsonb(r.get("config", {})), bool(r.get("active", True))))
            elif t == "rules_active":
                groups["rules_active"].append((list(r["rule_ids"]), sid))
            elif t == "line":
                groups["line"].append((sid, r["rule_id"], r["camera_id"], _ts(r["minute"]),
                                       int(r["count_in"]), int(r["count_out"])))
            elif t == "zone":
                groups["zone"].append((sid, r["rule_id"], r["camera_id"], _ts(r["minute"]), int(r["samples"]),
                                       float(r["avg_people"]), int(r["max_people"]),
                                       int(r["seconds_over_threshold"])))
            elif t == "alert":
                groups["alert"].append((r["alert_id"], sid, r["rule_id"], r["camera_id"], _ts(r["started_at"]),
                                        _ts(r.get("ended_at")), int(r["peak_people"]), int(r["threshold"]),
                                        _ts(r.get("notified_at")), r.get("notify_error")))
            else:
                log.warning("Registro de tipo desconocido ignorado: %r", t)
        try:
            conn = await self._connection()
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(_SQL_SITE_ENSURE, (sid, self.site_name))
                    for sql, key in ((_SQL_SITE, "site"), (_SQL_CAMERA, "camera"), (_SQL_RULE, "rule"),
                                     (_SQL_RULES_ACTIVE, "rules_active"), (_SQL_LINE, "line"),
                                     (_SQL_ZONE, "zone"), (_SQL_ALERT, "alert")):
                        if groups[key]:
                            await cur.executemany(sql, groups[key])  # type: ignore[arg-type]
        except psycopg.Error as exc:
            await self.close()
            raise StoreError(redact(f"{type(exc).__name__}: {exc}".strip())) from exc
        except OSError as exc:
            await self.close()
            raise StoreError(f"sin conexión con PostgreSQL: {exc}") from exc


def is_permanent(exc: StoreError) -> bool:
    """¿El error es de datos (no se arreglará reintentando)? Entonces el lote se aparta."""
    cause = exc.__cause__
    return isinstance(cause, (psycopg.errors.DataError, psycopg.errors.IntegrityError,
                              psycopg.errors.SyntaxError, psycopg.errors.UndefinedColumn))


class Persistence:
    """Cola en disco + PostgreSQL. `submit()` nunca lanza ni pierde datos; `drain()` los envía en orden.

    Si la cola en disco no se puede escribir (disco lleno, permisos), los lotes se guardan en
    memoria (con límite) y `drain()` los manda directamente a PostgreSQL o, si el disco vuelve a
    tener sitio, los pasa a la cola en disco. `disk_error` lo explica en status.json.
    """

    def __init__(self, spool: Spool, store: PgStore | None, *, max_memory_batches: int = MAX_MEMORY_BATCHES) -> None:
        self.spool = spool
        self.store = store
        self.last_error = "" if store else "PostgreSQL no configurado (VMS_PG_DSN vacío): los datos esperan en la cola local"
        self.ok = store is not None
        self.written_batches = 0
        self.disk_error = ""
        self.max_memory_batches = max_memory_batches
        self._memory: deque[list[Record]] = deque()
        self.dropped_batches = 0

    @property
    def memory_pending(self) -> int:
        return len(self._memory)

    def has_pending(self) -> bool:
        return bool(self._memory) or self.spool.pending() > 0

    def submit(self, records: list[Record]) -> None:
        if not records:
            return
        if self._memory:
            # Mantiene el orden: si ya hay lotes esperando en memoria, este va detrás.
            self._keep_in_memory(records)
            return
        try:
            self.spool.append(records)
        except OSError as exc:
            self._disk_failed(exc)
            self._keep_in_memory(records)
            return
        if self.disk_error:
            log.info("La cola local vuelve a poder escribirse en disco")
            self.disk_error = ""

    def _disk_failed(self, exc: OSError) -> None:
        msg = f"No se puede escribir la cola local en disco ({exc.strerror or exc}); los datos esperan en memoria"
        if msg != self.disk_error:
            log.error("%s: %s", msg, self.spool.dir)
        self.disk_error = msg

    def _keep_in_memory(self, records: list[Record]) -> None:
        self._memory.append(list(records))
        if len(self._memory) > self.max_memory_batches:
            self._memory.popleft()
            self.dropped_batches += 1
            log.error("Sin disco ni PostgreSQL durante demasiado tiempo: se descarta el lote más antiguo en memoria "
                      "(%d descartados)", self.dropped_batches)

    def _memory_to_disk(self) -> None:
        while self._memory:
            try:
                self.spool.append(self._memory[0])
            except OSError as exc:
                self._disk_failed(exc)
                return
            self._memory.popleft()
        if self.disk_error:
            log.info("La cola local vuelve a poder escribirse en disco")
            self.disk_error = ""

    def _store_failed(self, exc: StoreError) -> None:
        if self.ok or self.last_error != str(exc):
            log.warning("PostgreSQL no disponible (%s); %d lotes esperan en la cola local", exc,
                        self.spool.pending() + len(self._memory))
        self.ok = False
        self.last_error = str(exc)

    async def drain(self, max_files: int = 500) -> int:
        """Envía lotes pendientes en orden. Devuelve cuántos entraron. Se para al primer fallo."""
        if self._memory:
            self._memory_to_disk()
        if self.store is None:
            return 0
        done = 0
        for path in self.spool.head(max_files):
            try:
                records = self.spool.read(path)
            except FileNotFoundError:
                self.spool.remove(path)  # alguien lo quitó a mano: solo se olvida
                continue
            except (OSError, ValueError) as exc:
                self.spool.quarantine(path, f"ilegible: {exc}")
                continue
            try:
                await self.store.write(records)
            except StoreError as exc:
                if is_permanent(exc):
                    self.spool.quarantine(path, str(exc))
                    continue
                self._store_failed(exc)
                return done
            self.spool.remove(path)
            done += 1
            self.written_batches += 1
        # Lo que sigue en memoria (el disco sigue sin sitio) va directo a la base, en orden.
        while self._memory and self.spool.pending() == 0:
            try:
                await self.store.write(self._memory[0])
            except StoreError as exc:
                if is_permanent(exc):
                    log.error("Lote en memoria rechazado de forma permanente: se descarta (%s)", exc)
                    self._memory.popleft()
                    continue
                self._store_failed(exc)
                return done
            self._memory.popleft()
            done += 1
            self.written_batches += 1
        if done and not self.ok:
            log.info("PostgreSQL vuelve a responder: reenviados los lotes pendientes")
        if done or not self.has_pending():
            self.ok = True
            self.last_error = ""
        return done
