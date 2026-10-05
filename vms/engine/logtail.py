"""Lector incremental de `logs/engine.log` (modo `attach`, CONTRATO §13.10).

En modo `attach` el backend ya no lee la salida estándar de MediaMTX: la escribe `vmsctl run --service
VMSEngine` (ya sin credenciales) en `logs/engine.log`, con rotación `engine.log` → `engine.log.1` → …
Este lector la sigue para detectar las contraseñas rechazadas (401) y los errores de cada ruta.

- Abre, lee lo nuevo y **cierra** en cada vuelta: en Windows un archivo abierto sin `FILE_SHARE_DELETE`
  (lo normal en Python) impediría a `vmsctl` rotarlo.
- Detecta la rotación por la identidad del archivo (`st_ino`/`st_dev`, también en Windows) o porque
  encoge, y termina de leer lo que quedaba en `engine.log.1` antes de pasar al nuevo: no se pierden líneas.
- Solo entrega líneas completas; una línea a medio escribir espera a la siguiente vuelta.
- Empieza por el final: los errores antiguos (de antes de arrancar el backend) no vuelven a pausar equipos.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("vms.engine.logtail")

MAX_PENDING = 64 * 1024          # una «línea» sin salto más larga que esto se entrega cortada
MAX_READ = 4 * 1024 * 1024       # como mucho esto por vuelta (el resto, en la siguiente)

FileId = tuple[int, int]


def _file_id(st: os.stat_result) -> FileId:
    return (st.st_dev, st.st_ino)


class LogTail:
    def __init__(self, path: Path, on_line: Callable[[str], None], *, start_at_end: bool = True) -> None:
        self.path = Path(path)
        self.on_line = on_line
        self._id: FileId | None = None
        self._offset = 0
        self._pending = b""
        self._started = False
        self._start_at_end = start_at_end
        self.lines_read = 0
        self.rotations = 0

    def _emit(self, chunk: bytes, out: list[str]) -> None:
        data = self._pending + chunk
        *lines, rest = data.split(b"\n")
        if len(rest) > MAX_PENDING:
            lines.append(rest)
            rest = b""
        self._pending = rest
        for raw in lines:
            line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
            if line:
                out.append(line)

    def _read_from(self, path: Path, offset: int) -> tuple[bytes, int]:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(MAX_READ)
        return data, offset + len(data)

    def _rotated_tail(self, out: list[str]) -> None:
        """Lo que quedaba por leer del archivo anterior, si sigue ahí como `engine.log.1`."""
        old = self.path.with_name(self.path.name + ".1")
        try:
            st = old.stat()
            if self._id is not None and _file_id(st) == self._id and st.st_size > self._offset:
                data, _ = self._read_from(old, self._offset)
                self._emit(data, out)
        except OSError:
            pass
        if self._pending:
            self._emit(b"\n", out)  # la última línea del archivo viejo ya no va a completarse

    def read_new(self) -> list[str]:
        """Solo E/S (se puede llamar desde un hilo): las líneas nuevas completas."""
        out: list[str] = []
        try:
            st = self.path.stat()
        except FileNotFoundError:
            # Si aún no existe, lo que se escriba cuando aparezca es nuevo: se leerá desde el principio.
            self._started = True
            return out
        except OSError as exc:
            log.debug("No se puede leer %s: %s", self.path, exc)
            return out
        fid = _file_id(st)
        if not self._started:
            self._started = True
            self._id = fid
            self._offset = st.st_size if self._start_at_end else 0
            if self._start_at_end:
                return out
        if fid != self._id or st.st_size < self._offset:
            if self._id is not None:
                self.rotations += 1
                self._rotated_tail(out)
            self._id = fid
            self._offset = 0
        if st.st_size > self._offset:
            try:
                data, self._offset = self._read_from(self.path, self._offset)
            except OSError as exc:
                log.debug("No se pudo leer %s: %s", self.path, exc)
                return out
            self._emit(data, out)
        self.lines_read += len(out)
        return out

    def dispatch(self, lines: list[str]) -> None:
        for line in lines:
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001 - un fallo al interpretar no debe cortar la lectura
                log.exception("Error procesando una línea de engine.log")

    def poll(self) -> int:
        """Una vuelta en el hilo actual: lee y entrega. Devuelve cuántas líneas."""
        lines = self.read_new()
        self.dispatch(lines)
        return len(lines)

    async def run(self, interval: float = 1.0) -> None:
        """Lee en un hilo y entrega las líneas en el bucle de eventos (el motor crea tareas al pausar)."""
        while True:
            try:
                self.dispatch(await asyncio.to_thread(self.read_new))
            except Exception:  # noqa: BLE001 - el lector no puede morir
                log.exception("Error siguiendo engine.log")
            await asyncio.sleep(interval)
