"""Almacén de B6 en `<datos>/ops/ops.sqlite3` (CONTRATO §18.17).

SQLite de la biblioteca estándar en modo WAL, con un solo escritor (el backend). Solo guarda
**metadatos**: medidas numéricas de salud, medidas de hora, marcadores, exportaciones, registro de avisos
y eventos de la línea de tiempo. Nunca imágenes ni datos de personas (RGPD, docs/RGPD-EIPD.md §2.5).

Conservación: `health_checks` 90 días, `clock_checks` 400 días, `notifications_log` 90 días y
`timeline_events` 400 días (`prune`). Los marcadores y las exportaciones se conservan mientras existan.

Fechas: texto ISO 8601 en UTC con microsegundos (`2026-10-05T10:00:00.000000+00:00`); así el orden de
texto coincide con el orden temporal y las consultas por intervalo usan el índice.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import (Bookmark, ClockCheck, EvidenceExport, HealthCheck, NotificationRecord, TimelineEvent)

log = logging.getLogger("vms.ops.store")

SCHEMA_VERSION = 1
RETENTION_DAYS = {"health_checks": 90, "clock_checks": 400, "notifications_log": 90, "timeline_events": 400}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    at TEXT NOT NULL,
    score INTEGER,
    status TEXT NOT NULL,
    causes TEXT NOT NULL DEFAULT '[]',
    metrics TEXT NOT NULL DEFAULT '{}',
    reference TEXT NOT NULL DEFAULT 'none',
    duration_ms REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS health_checks_cam_at ON health_checks (camera_id, at);
CREATE TABLE IF NOT EXISTS clock_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT,
    device_id TEXT,
    at TEXT NOT NULL,
    skew_s REAL,
    round_trip_ms REAL,
    time_mode TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'unknown',
    message_es TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS clock_checks_dev_at ON clock_checks (device_id, at);
CREATE TABLE IF NOT EXISTS bookmarks (
    id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL,
    start TEXT NOT NULL,
    "end" TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmarks_cam_start ON bookmarks (camera_id, start);
CREATE TABLE IF NOT EXISTS evidence_exports (
    export_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications_log (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_log_at ON notifications_log (at);
CREATE TABLE IF NOT EXISTS timeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    layer TEXT NOT NULL,
    start TEXT NOT NULL,
    "end" TEXT,
    severity TEXT NOT NULL DEFAULT 'info',
    title_es TEXT NOT NULL,
    ref_id TEXT
);
CREATE INDEX IF NOT EXISTS timeline_events_cam_start ON timeline_events (camera_id, start);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def iso(dt: datetime) -> str:
    """Texto ordenable de una fecha (UTC, microsegundos)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class OpsStore:
    """Acceso sincrónico y seguro entre hilos (un cerrojo); las rutas lo llaman con `asyncio.to_thread`
    cuando la consulta puede ser larga."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _all(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _one(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    # ------------------------------------------------------------------ salud de imagen
    def add_health_check(self, check: HealthCheck) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO health_checks (camera_id, at, score, status, causes, metrics, reference, duration_ms) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (check.camera_id, iso(check.at), check.score, check.status,
                       json.dumps([str(x) for x in check.causes]), check.metrics.model_dump_json(exclude_none=True),
                       check.reference, round(check.duration_ms, 3)))

    def health_checks(self, camera_id: str | None, start: datetime, end: datetime) -> list[HealthCheck]:
        sql = "SELECT * FROM health_checks WHERE at >= ? AND at < ?"
        params: list[Any] = [iso(start), iso(end)]
        if camera_id is not None:
            sql += " AND camera_id = ?"
            params.append(camera_id)
        rows = self._all(sql + " ORDER BY at", tuple(params))
        return [HealthCheck.model_validate({"camera_id": r["camera_id"], "at": r["at"], "score": r["score"],
                                            "status": r["status"], "causes": json.loads(r["causes"]),
                                            "metrics": json.loads(r["metrics"]), "reference": r["reference"],
                                            "duration_ms": r["duration_ms"]}) for r in rows]

    def health_min_scores(self, start: datetime, end: datetime) -> dict[str, tuple[int | None, list[str]]]:
        """Por cámara: puntuación mínima del intervalo y las causas de esa comprobación."""
        out: dict[str, tuple[int | None, list[str]]] = {}
        rows = self._all("SELECT camera_id, score, causes FROM health_checks WHERE at >= ? AND at < ? "
                         "AND score IS NOT NULL ORDER BY camera_id, score ASC, at ASC", (iso(start), iso(end)))
        for r in rows:
            if r["camera_id"] not in out:
                out[r["camera_id"]] = (r["score"], json.loads(r["causes"]))
        return out

    # ------------------------------------------------------------------ hora
    def add_clock_check(self, check: ClockCheck) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO clock_checks (camera_id, device_id, at, skew_s, round_trip_ms, time_mode, status, "
                      "message_es) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (check.camera_id, check.device_id, iso(check.at), check.skew_s, check.round_trip_ms,
                       check.time_mode, check.status, check.message_es))

    def latest_clock_checks(self) -> dict[str | None, ClockCheck]:
        """Última medida por equipo (clave None = el propio PC)."""
        rows = self._all("SELECT c.* FROM clock_checks c JOIN (SELECT coalesce(device_id, '') d, max(id) m "
                         "FROM clock_checks GROUP BY coalesce(device_id, '')) x ON c.id = x.m")
        out: dict[str | None, ClockCheck] = {}
        for r in rows:
            out[r["device_id"]] = self._clock(r)
        return out

    def clock_checks(self, device_id: str | None, start: datetime, end: datetime) -> list[ClockCheck]:
        if device_id is None:
            rows = self._all("SELECT * FROM clock_checks WHERE device_id IS NULL AND at >= ? AND at < ? ORDER BY at",
                             (iso(start), iso(end)))
        else:
            rows = self._all("SELECT * FROM clock_checks WHERE device_id = ? AND at >= ? AND at < ? ORDER BY at",
                             (device_id, iso(start), iso(end)))
        return [self._clock(r) for r in rows]

    @staticmethod
    def _clock(r: sqlite3.Row) -> ClockCheck:
        return ClockCheck.model_validate({k: r[k] for k in ("camera_id", "device_id", "at", "skew_s", "round_trip_ms",
                                                            "time_mode", "status", "message_es")})

    # ------------------------------------------------------------------ marcadores
    def save_bookmark(self, bm: Bookmark) -> None:
        with self._tx() as c:
            c.execute('INSERT OR REPLACE INTO bookmarks (id, camera_id, start, "end", data) VALUES (?, ?, ?, ?, ?)',
                      (bm.id, bm.camera_id, iso(bm.start), iso(bm.end) if bm.end else None, bm.model_dump_json()))

    def get_bookmark(self, bookmark_id: str) -> Bookmark | None:
        r = self._one("SELECT data FROM bookmarks WHERE id = ?", (bookmark_id,))
        return Bookmark.model_validate_json(r["data"]) if r else None

    def delete_bookmark(self, bookmark_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))

    def bookmarks(self, camera_id: str | None = None, start: datetime | None = None,
                  end: datetime | None = None) -> list[Bookmark]:
        sql = "SELECT data FROM bookmarks WHERE 1=1"
        params: list[Any] = []
        if camera_id is not None:
            sql += " AND camera_id = ?"
            params.append(camera_id)
        if end is not None:
            sql += " AND start < ?"
            params.append(iso(end))
        if start is not None:
            sql += ' AND coalesce("end", start) >= ?'
            params.append(iso(start))
        return [Bookmark.model_validate_json(r["data"]) for r in self._all(sql + " ORDER BY start", tuple(params))]

    # ------------------------------------------------------------------ exportaciones
    def save_export(self, exp: EvidenceExport) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO evidence_exports (export_id, created_at, data) VALUES (?, ?, ?)",
                      (exp.export_id, iso(exp.created_at), exp.model_dump_json()))

    def get_export(self, export_id: str) -> EvidenceExport | None:
        r = self._one("SELECT data FROM evidence_exports WHERE export_id = ?", (export_id,))
        return EvidenceExport.model_validate_json(r["data"]) if r else None

    def exports(self, limit: int = 50) -> list[EvidenceExport]:
        rows = self._all("SELECT data FROM evidence_exports ORDER BY created_at DESC LIMIT ?", (limit,))
        return [EvidenceExport.model_validate_json(r["data"]) for r in rows]

    def delete_export(self, export_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM evidence_exports WHERE export_id = ?", (export_id,))

    # ------------------------------------------------------------------ avisos
    def add_notification(self, rec: NotificationRecord) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO notifications_log (id, at, data) VALUES (?, ?, ?)",
                      (rec.id, iso(rec.at), rec.model_dump_json()))

    def notifications(self, limit: int = 200) -> list[NotificationRecord]:
        rows = self._all("SELECT data FROM notifications_log ORDER BY at DESC LIMIT ?", (limit,))
        return [NotificationRecord.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------------ línea de tiempo
    def add_timeline_event(self, ev: TimelineEvent) -> None:
        with self._tx() as c:
            c.execute('INSERT INTO timeline_events (camera_id, layer, start, "end", severity, title_es, ref_id) '
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (ev.camera_id, ev.layer, iso(ev.start), iso(ev.end) if ev.end else None, ev.severity,
                       ev.title_es, ev.ref_id))

    def close_open_timeline_event(self, camera_id: str, layer: str, end: datetime) -> None:
        with self._tx() as c:
            c.execute('UPDATE timeline_events SET "end" = ? WHERE id = (SELECT max(id) FROM timeline_events '
                      'WHERE camera_id = ? AND layer = ? AND "end" IS NULL)', (iso(end), camera_id, layer))

    def timeline_events(self, camera_id: str, start: datetime, end: datetime,
                        layers: set[str] | None = None) -> list[TimelineEvent]:
        rows = self._all('SELECT * FROM timeline_events WHERE camera_id = ? AND start < ? '
                         'AND coalesce("end", ?) >= ? ORDER BY start', (camera_id, iso(end), iso(end), iso(start)))
        out = []
        for r in rows:
            if layers is not None and r["layer"] not in layers:
                continue
            out.append(TimelineEvent.model_validate({"camera_id": r["camera_id"], "layer": r["layer"],
                                                     "start": r["start"], "end": r["end"], "severity": r["severity"],
                                                     "title_es": r["title_es"], "ref_id": r["ref_id"]}))
        return out

    # ------------------------------------------------------------------ varios
    def kv_get(self, key: str) -> Any:
        r = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(r["value"]) if r else None

    def kv_set(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                      (key, json.dumps(value, ensure_ascii=False, default=str)))

    def counts(self) -> dict[str, int]:
        """Recuentos por tabla (lo único de B6 que puede ir en un diagnóstico, CONTRATO §18.17)."""
        out = {}
        for table in ("health_checks", "clock_checks", "bookmarks", "evidence_exports", "notifications_log",
                      "timeline_events"):
            r = self._one(f"SELECT count(*) AS n FROM {table}")   # noqa: S608 - nombres fijos de esta lista
            out[table] = int(r["n"]) if r else 0
        return out

    def prune(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        removed = {}
        with self._tx() as c:
            for table, days in RETENTION_DAYS.items():
                col = "start" if table == "timeline_events" else "at"
                cur = c.execute(f"DELETE FROM {table} WHERE {col} < ?",   # noqa: S608 - nombres fijos
                                (iso(now - timedelta(days=days)),))
                removed[table] = cur.rowcount
        return removed
