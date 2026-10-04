"""Agregación por minuto: lo único que se guarda de la analítica son estos números.

Por cada regla y cada minuto (UTC, truncado):
- Línea de puerta: `count_in` (entradas) y `count_out` (salidas).
- Zona de cola: `samples` (imágenes analizadas), `avg_people` (ocupación media), `max_people`
  (máxima) y `seconds_over_threshold` (segundos con la cola en o por encima del umbral de aviso).

Un minuto se «cierra» y se envía a PostgreSQL cuando ya pasó (más un margen de 5 s para las
últimas imágenes en vuelo). Se escribe una sola vez con un upsert idempotente: si hay que
reintentarlo, no se duplica nada.

Las líneas se escriben aunque el minuto tenga 0 entradas: así el informe distingue «no entró
nadie» de «la cámara no estaba funcionando» (en ese caso no hay fila).

Si el proceso se para con un minuto a medias, ese minuto se guarda en disco y se recupera al
arrancar (`export_open` / `import_open`), para no sobrescribir el minuto con un conteo parcial.

Si el reloj del PC retrocede (corrección de NTP/w32time) y llegan muestras con la hora de un
minuto que ya se cerró y se envió, NO se abre otra vez ese minuto (el upsert lo sobrescribiría con
un conteo parcial): las muestras se suman al primer minuto todavía abierto.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

Record = dict[str, Any]

log = logging.getLogger("analytics.aggregation")


def minute_of(ts: float) -> datetime:
    """Minuto UTC (truncado) al que pertenece un instante time.time()."""
    return datetime.fromtimestamp(int(ts // 60) * 60, tz=timezone.utc)


@dataclass
class _LineAcc:
    camera_id: str
    count_in: int = 0
    count_out: int = 0


@dataclass
class _ZoneAcc:
    camera_id: str
    samples: int = 0
    sum_people: float = 0.0
    max_people: int = 0
    seconds_over: float = 0.0


class MinuteAggregator:
    """Acumula conteos por (regla, minuto). Seguro para usar desde varios hilos."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: dict[tuple[str, datetime], _LineAcc] = {}
        self._zones: dict[tuple[str, datetime], _ZoneAcc] = {}
        self._closed_upto: datetime | None = None   # último minuto ya cerrado y enviado
        self.late_samples = 0

    def _minute(self, ts: float) -> datetime:
        """Minuto de una muestra; nunca uno ya cerrado (llamar con el cerrojo tomado)."""
        minute = minute_of(ts)
        if self._closed_upto is not None and minute <= self._closed_upto:
            self.late_samples += 1
            if self.late_samples == 1 or self.late_samples % 1000 == 0:
                log.warning("Muestras con la hora de un minuto ya enviado (%s): el reloj del equipo retrocedió. "
                            "Se suman al minuto abierto siguiente (%d muestras hasta ahora)",
                            minute.isoformat(), self.late_samples)
            minute = self._closed_upto + timedelta(minutes=1)
        return minute

    # ------------------------------------------------------------------ entrada
    def add_line(self, rule_id: str, camera_id: str, ts: float, n_in: int, n_out: int) -> None:
        with self._lock:
            key = (rule_id, self._minute(ts))
            acc = self._lines.get(key)
            if acc is None:
                acc = self._lines[key] = _LineAcc(camera_id)
            acc.count_in += n_in
            acc.count_out += n_out

    def add_zone(self, rule_id: str, camera_id: str, ts: float, occupancy: int, over_threshold: bool,
                 dt_seconds: float) -> None:
        """Una muestra de ocupación. `dt_seconds` = tiempo que representa (≈ 1/fps)."""
        with self._lock:
            key = (rule_id, self._minute(ts))
            acc = self._zones.get(key)
            if acc is None:
                acc = self._zones[key] = _ZoneAcc(camera_id)
            acc.samples += 1
            acc.sum_people += occupancy
            acc.max_people = max(acc.max_people, occupancy)
            if over_threshold:
                acc.seconds_over += max(0.0, dt_seconds)

    # ------------------------------------------------------------------ salida
    def pop_closed(self, now: float, margin_seconds: float = 5.0) -> list[Record]:
        """Registros de los minutos ya cerrados (fin del minuto + margen <= now)."""
        limit = datetime.fromtimestamp(now - margin_seconds, tz=timezone.utc) - timedelta(minutes=1)
        return self._pop(lambda m: m <= limit)

    def pop_all(self) -> list[Record]:
        return self._pop(lambda m: True)

    def _pop(self, pred: Any) -> list[Record]:
        out: list[Record] = []
        with self._lock:
            popped = [k[1] for k in self._lines if pred(k[1])] + [k[1] for k in self._zones if pred(k[1])]
            if popped:
                newest = max(popped)
                if self._closed_upto is None or newest > self._closed_upto:
                    self._closed_upto = newest
            for key in sorted(k for k in self._lines if pred(k[1])):
                acc = self._lines.pop(key)
                out.append(line_record(key[0], acc.camera_id, key[1], acc.count_in, acc.count_out))
            for key in sorted(k for k in self._zones if pred(k[1])):
                z = self._zones.pop(key)
                if z.samples <= 0:
                    continue
                out.append(zone_record(key[0], z.camera_id, key[1], z.samples, z.sum_people / z.samples,
                                       z.max_people, z.seconds_over))
        return out

    def open_minutes(self) -> int:
        with self._lock:
            return len(self._lines) + len(self._zones)

    # ------------------------------------------------------------------ persistencia del minuto en curso
    def export_open(self) -> list[Record]:
        """Estado bruto (sin cerrar) para guardarlo en disco al parar."""
        out: list[Record] = []
        with self._lock:
            for (rule_id, minute), a in self._lines.items():
                out.append({"type": "line_open", "rule_id": rule_id, "camera_id": a.camera_id,
                            "minute": minute.isoformat(), "count_in": a.count_in, "count_out": a.count_out})
            for (rule_id, minute), z in self._zones.items():
                out.append({"type": "zone_open", "rule_id": rule_id, "camera_id": z.camera_id,
                            "minute": minute.isoformat(), "samples": z.samples, "sum_people": z.sum_people,
                            "max_people": z.max_people, "seconds_over": z.seconds_over})
        return out

    def import_open(self, records: list[Record]) -> int:
        """Recupera minutos a medias guardados con export_open (se suman a lo que haya)."""
        n = 0
        with self._lock:
            for r in records:
                minute = datetime.fromisoformat(r["minute"])
                key = (str(r["rule_id"]), minute)
                if r.get("type") == "line_open":
                    acc = self._lines.setdefault(key, _LineAcc(str(r["camera_id"])))
                    acc.count_in += int(r["count_in"])
                    acc.count_out += int(r["count_out"])
                    n += 1
                elif r.get("type") == "zone_open":
                    z = self._zones.setdefault(key, _ZoneAcc(str(r["camera_id"])))
                    z.samples += int(r["samples"])
                    z.sum_people += float(r["sum_people"])
                    z.max_people = max(z.max_people, int(r["max_people"]))
                    z.seconds_over += float(r["seconds_over"])
                    n += 1
        return n


def line_record(rule_id: str, camera_id: str, minute: datetime, count_in: int, count_out: int) -> Record:
    return {"type": "line", "rule_id": rule_id, "camera_id": camera_id, "minute": minute.isoformat(),
            "count_in": int(count_in), "count_out": int(count_out)}


def zone_record(rule_id: str, camera_id: str, minute: datetime, samples: int, avg_people: float,
                max_people: int, seconds_over: float) -> Record:
    return {"type": "zone", "rule_id": rule_id, "camera_id": camera_id, "minute": minute.isoformat(),
            "samples": int(samples), "avg_people": round(float(avg_people), 3), "max_people": int(max_people),
            "seconds_over_threshold": int(min(60, max(0, round(seconds_over))))}
