"""Por qué no funciona el motor en modo attach (vms.engine.problem, CONTRATO §6.8 y §13.10).

El fallo real: en el PC del usuario MediaMTX se cerraba al arrancar porque HikCentral (SYS.exe) tenía el puerto
127.0.0.1:9998 y Estado del sistema solo decía «no responde; revisa «vmsctl services status» y logs/engine.log».
`REAL_CRASH_BLOCK` son las primeras líneas de su engine.log (paquete de diagnóstico, ya sin credenciales).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.test_spanish_style import voseo_in
from vms.core.errors import EngineUnavailable
from vms.core.interfaces import EngineProblem
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings
from vms.engine import MediaMtxEngine
from vms.engine import problem as P
from vms.engine.logtail import LogTail
from vms.engine.mtx_api import MtxApiError
from vms.engine.process import MediaMtxProcess

REAL_CRASH_BLOCK = r"""2026-10-06T09:05:59Z [vmsctl] VMSEngine: arranco C:\Program Files\VMSMultimarca\versions\2.0.0-beta.1\engine\mediamtx.exe (pid 34844)
2026-10-06T09:06:04Z [vmsctl] proceso en marcha (pid 49336)
2026/10/06 11:06:04 INF MediaMTX v1.21.1, windows, amd64
2026/10/06 11:06:04 INF configuration loaded from C:\ProgramData\VMSMultimarca\mediamtx\mediamtx.yml
2026/10/06 11:06:04 ERR listen tcp 127.0.0.1:9998: bind: Only one usage of each socket address (protocol/network address/port) is normally permitted.
2026/10/06 11:06:04 INF waiting for running hooks
2026-10-06T09:06:04Z [vmsctl] el proceso terminó con código 1 tras 0 s
2026-10-06T09:06:04Z [vmsctl] lo relanzo dentro de 1 s
2026-10-06T09:06:06Z [vmsctl] proceso en marcha (pid 49576)
2026/10/06 11:06:06 INF MediaMTX v1.21.1, windows, amd64
2026/10/06 11:06:06 INF configuration loaded from C:\ProgramData\VMSMultimarca\mediamtx\mediamtx.yml
2026/10/06 11:06:06 ERR listen tcp 127.0.0.1:9998: bind: Only one usage of each socket address (protocol/network address/port) is normally permitted.
2026/10/06 11:06:06 INF waiting for running hooks
2026-10-06T09:06:06Z [vmsctl] el proceso terminó con código 1 tras 0 s
2026-10-06T09:06:06Z [vmsctl] lo relanzo dentro de 2 s
2026-10-06T09:06:08Z [vmsctl] proceso en marcha (pid 41860)
2026/10/06 11:06:08 INF MediaMTX v1.21.1, windows, amd64
2026/10/06 11:06:08 INF configuration loaded from C:\ProgramData\VMSMultimarca\mediamtx\mediamtx.yml
2026/10/06 11:06:08 ERR listen tcp 127.0.0.1:9998: bind: Only one usage of each socket address (protocol/network address/port) is normally permitted.
2026/10/06 11:06:08 INF waiting for running hooks
2026-10-06T09:06:08Z [vmsctl] el proceso terminó con código 1 tras 0 s
2026-10-06T09:06:08Z [vmsctl] lo relanzo dentro de 5 s
2026-10-06T09:06:13Z [vmsctl] proceso en marcha (pid 47628)
2026/10/06 11:06:14 INF MediaMTX v1.21.1, windows, amd64
2026/10/06 11:06:14 INF configuration loaded from C:\ProgramData\VMSMultimarca\mediamtx\mediamtx.yml
2026/10/06 11:06:14 ERR listen tcp 127.0.0.1:9998: bind: Only one usage of each socket address (protocol/network address/port) is normally permitted.
2026/10/06 11:06:14 INF waiting for running hooks
2026-10-06T09:06:14Z [vmsctl] el proceso terminó con código 1 tras 0 s
2026-10-06T09:06:14Z [vmsctl] lo relanzo dentro de 10 s
2026-10-06T09:06:24Z [vmsctl] proceso en marcha (pid 20072)
2026/10/06 11:06:24 INF MediaMTX v1.21.1, windows, amd64
2026/10/06 11:06:24 INF configuration loaded from C:\ProgramData\VMSMultimarca\mediamtx\mediamtx.yml
2026/10/06 11:06:24 ERR listen tcp 127.0.0.1:9998: bind: Only one usage of each socket address (protocol/network address/port) is normally permitted.
2026/10/06 11:06:24 INF waiting for running hooks
2026-10-06T09:06:24Z [vmsctl] el proceso terminó con código 1 tras 0 s
2026-10-06T09:06:24Z [vmsctl] lo relanzo dentro de 30 s
"""
LAST_EXIT = datetime(2026, 10, 6, 9, 6, 24, tzinfo=timezone.utc).timestamp()

# MediaMTX v1.21.1 con un mediamtx.yml que no entiende (comprobado ejecutándolo): «ERR: …» sin fecha
CONFIG_ERROR_BLOCK = """2026-10-07T10:00:00Z [vmsctl] proceso en marcha (pid 100)
ERR: json: unknown field "campoInventado"
2026-10-07T10:00:00Z [vmsctl] el proceso terminó con código 1 tras 0 s
2026-10-07T10:00:00Z [vmsctl] lo relanzo dentro de 1 s
"""

# Un error inofensivo con el motor sano (13 veces en el log del usuario) y luego un «panic» de Go con su pila
PANIC_BLOCK = ("2026-10-07T10:00:00Z [vmsctl] proceso en marcha (pid 200)\n"
               "2026/10/07 12:00:01 INF MediaMTX v1.21.1, windows, amd64\n"
               "2026/10/07 12:00:05 ERR [playback] GetFileAttributesEx C:\\x: The system cannot find the file.\n"
               + "".join(f"2026/10/07 12:{i:02d}:00 INF [path cam-1/main] [recorder] recording segment\n"
                         for i in range(1, 30))
               + "panic: runtime error: invalid memory address or nil pointer dereference\n"
               + "".join(f"\tgithub.com/bluenviron/mediamtx/internal/core.(*path).run+0x{i:x}\n" for i in range(20))
               + "2026-10-07T10:30:00Z [vmsctl] el proceso terminó con código 2 tras 1800 s\n")

# MediaMTX v1.21.1 de verdad (bin\mediamtx.exe, 8/10/2026) con 12 cámaras grabando de un NVR y el puerto de la API
# ocupado; solo se han cambiado los puertos aleatorios de la prueba por los de una instalación. Las rutas arrancan
# antes que RTSP, WebRTC y la API, así que entre el «bind» y la salida hay 28 líneas de las cámaras: con 4 o más,
# el motivo se perdía y Estado del sistema decía «no ha dejado ningún mensaje de error».
MANY_CAMERAS_BLOCK = r"""2026-10-08T14:03:41Z [vmsctl] proceso en marcha (pid 51000)
2026/10/08 16:03:40 INF MediaMTX v1.21.1, windows, amd64
2026/10/08 16:03:41 INF configuration loaded from C:\ProgramData\VMSMultimarca\mediamtx\mediamtx.yml
2026/10/08 16:03:41 INF [playback] started with listener on 127.0.0.1:9996 (TCP/HTTP)
2026/10/08 16:03:41 INF [path cam-00000010/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000008/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000006/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000003/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000002/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000007/main] [RTSP source] started
2026/10/08 16:03:41 INF [RTSP] started with listeners on 127.0.0.1:8554 (TCP/RTSP)
2026/10/08 16:03:41 INF [path cam-00000001/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000004/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000011/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000005/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000000/main] [RTSP source] started
2026/10/08 16:03:41 INF [path cam-00000009/main] [RTSP source] started
2026/10/08 16:03:41 INF [WebRTC] started with listeners on 127.0.0.1:8889 (TCP/HTTP), :8189 (UDP/ICE)
2026/10/08 16:03:41 ERR listen tcp 127.0.0.1:9997: bind: Only one usage of each socket address (protocol/network address/port) is normally permitted.
2026/10/08 16:03:41 INF [WebRTC] closing
2026/10/08 16:03:41 ERR [path cam-00000000/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 ERR [path cam-00000007/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 ERR [path cam-00000010/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 ERR [path cam-00000005/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 ERR [path cam-00000004/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 ERR [path cam-00000008/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 ERR [path cam-00000006/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 INF [RTSP] closing
2026/10/08 16:03:41 ERR [path cam-00000011/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 ERR [path cam-00000009/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 INF [path cam-00000008/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000010/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000011/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000003/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000009/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 ERR [path cam-00000003/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 INF [path cam-00000001/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000002/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 ERR [path cam-00000002/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 INF [path cam-00000000/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000006/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000007/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000005/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 INF [path cam-00000004/main] [RTSP source] stopped: path is closing
2026/10/08 16:03:41 ERR [path cam-00000001/main] [RTSP source] dial tcp 192.0.2.10:554: connectex: No connection could be made because the target machine actively refused it.
2026/10/08 16:03:41 INF [playback] closing
2026/10/08 16:03:41 INF waiting for running hooks
2026-10-08T14:03:41Z [vmsctl] el proceso terminó con código 1 tras 0 s
2026-10-08T14:03:41Z [vmsctl] lo relanzo dentro de 30 s
"""
MANY_EXIT = datetime(2026, 10, 8, 14, 3, 41, tzinfo=timezone.utc).timestamp()

# También real: al recargar mediamtx.yml con un puerto ocupado, MediaMTX se cierra (¡con código 0!) y antes cierra
# cada conexión abierta, una línea por conexión (aquí 12 lectores RTSP)
RELOAD_BLOCK = ("2026-10-08T13:00:00Z [vmsctl] proceso en marcha (pid 52000)\n"
                "2026/10/08 15:00:00 INF MediaMTX v1.21.1, windows, amd64\n"
                "2026/10/08 15:59:20 INF reloading configuration (file changed)\n"
                "2026/10/08 15:59:20 INF [API] closing\n"
                "2026/10/08 15:59:20 ERR listen tcp 127.0.0.1:9997: bind: Only one usage of each socket address "
                "(protocol/network address/port) is normally permitted.\n"
                "2026/10/08 15:59:20 INF [RTSP] closing\n"
                + "".join(f"2026/10/08 15:59:20 INF [RTSP] [conn 127.0.0.1:{63258 + i}] closed: terminated\n"
                          for i in range(12))
                + "2026/10/08 15:59:20 INF waiting for running hooks\n"
                "2026-10-08T13:59:20Z [vmsctl] el proceso terminó con código 0 tras 3560 s\n")

Conn = namedtuple("Conn", "fd family type laddr raddr status pid")
Addr = namedtuple("Addr", "ip port")


def feed(block: str) -> P.LogCause:
    c = P.EngineLogCause()
    for line in block.splitlines():
        c.feed(line)
    return c.snapshot()


def fake_psutil(monkeypatch: pytest.MonkeyPatch, conns: list[Conn], names: dict[int, Any]) -> None:
    """psutil de mentira: `names[pid]` es el nombre o la excepción que da Process(pid).name()."""
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": conns)

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def name(self) -> str:
            v = names[self.pid]
            if isinstance(v, Exception):
                raise v
            return str(v)

    monkeypatch.setattr(psutil, "Process", FakeProcess)


SYS_ON_9998 = [Conn(1, socket.AF_INET, socket.SOCK_STREAM, Addr("127.0.0.1", 9998), (), psutil.CONN_LISTEN, 6712)]


def check_texts(p: EngineProblem) -> None:
    assert p.detail_es and p.action_es
    assert not voseo_in(p.detail_es + " " + p.action_es), p
    assert "Cl4ve" not in p.detail_es + p.action_es


# ----------------------------------------------------------------------------------------------- engine.log
def test_bind_error_formats() -> None:
    real = ("listen tcp 127.0.0.1:9998: bind: Only one usage of each socket address (protocol/network address/port) "
            "is normally permitted.")
    assert P.BindError.parse(real) == P.BindError("tcp", "127.0.0.1", 9998, "in_use")
    assert P.BindError.parse("listen udp :8189: bind: An attempt was made to access a socket in a way forbidden by its "
                             "access permissions.") == P.BindError("udp", "", 8189, "forbidden")
    assert P.BindError.parse("listen tcp [::1]:8554: bind: address already in use") == P.BindError("tcp", "::1", 8554,
                                                                                                   "in_use")
    assert P.BindError.parse("[path cam/main] [RTSP source] dial tcp 10.0.0.5:554: i/o timeout") is None
    # Windows sin los textos en inglés: Go los da en el idioma del sistema o como número
    for text in ("Solo se permite un uso de cada dirección de socket (protocolo/dirección de red/puerto)",
                 "winapi error #10048"):
        assert P.BindError.parse(f"listen tcp 127.0.0.1:9997: bind: {text}") == P.BindError("tcp", "127.0.0.1", 9997,
                                                                                            "in_use")
    # 10049: la dirección ya no es de este equipo. No es un puerto ocupado
    assert P.BindError.parse("listen tcp 192.168.1.80:8554: bind: The requested address is not valid in its "
                             "context.") == P.BindError("tcp", "192.168.1.80", 8554, "other")


def test_log_cause_reads_the_real_crash_block() -> None:
    c = feed(REAL_CRASH_BLOCK)
    assert (c.state, c.exit_code) == ("exited", 1)
    assert "listen tcp 127.0.0.1:9998: bind" in c.exit_error
    assert c.crashes(LAST_EXIT + 1) == 5 and c.crashes(LAST_EXIT + 700) == 0


def test_an_old_error_of_a_healthy_run_is_not_the_cause_but_a_panic_is() -> None:
    c = feed(PANIC_BLOCK)
    assert c.exit_error.startswith("panic: runtime error") and c.exit_code == 2
    healthy = PANIC_BLOCK.replace("panic: runtime error: invalid memory address or nil pointer dereference\n", "")
    healthy = "".join(ln + "\n" for ln in healthy.splitlines() if not ln.startswith("\t"))
    assert feed(healthy).exit_error == "", "un «[playback] …» de hace media hora no explica la caída"


def test_config_error_and_engine_run_format() -> None:
    assert feed(CONFIG_ERROR_BLOCK).exit_error == 'ERR: json: unknown field "campoInventado"'
    dev = ("2026-10-08T14:55:43 [engine-run] MediaMTX en marcha (pid 7)\n"
           "2026/10/08 14:55:43 ERR listen tcp 127.0.0.1:5555: bind: address already in use\n"
           "2026-10-08T14:55:43 [engine-run] MediaMTX terminó (código 1); se relanza en 1 s\n")
    c = feed(dev)
    assert c.state == "exited" and c.exit_code == 1 and ":5555: bind" in c.exit_error


def test_the_bind_error_survives_the_lines_of_many_cameras_and_connections() -> None:
    c = feed(MANY_CAMERAS_BLOCK)
    assert c.exit_code == 1 and "listen tcp 127.0.0.1:9997: bind" in c.exit_error, c
    # Con más cámaras todavía (el doble de líneas entre el «bind» y la salida)
    lines = MANY_CAMERAS_BLOCK.splitlines()
    cut = next(i for i, ln in enumerate(lines) if "[WebRTC] closing" in ln)
    noisy = "\n".join(lines[:cut] + [ln.replace("cam-000", "cam-100") for ln in lines[cut:-3] if "[path " in ln]
                      + lines[cut:])
    assert ":9997: bind" in feed(noisy).exit_error
    # Al recargar mediamtx.yml: una línea por conexión abierta y salida con código 0
    c = feed(RELOAD_BLOCK)
    assert c.exit_code == 0 and ":9997: bind" in c.exit_error, c
    # El «bind» de una sesión no cierra MediaMTX: horas después no es la causa de una caída sin mensaje
    session = ("2026-10-08T10:00:00Z [vmsctl] proceso en marcha (pid 400)\n"
               "2026/10/08 12:00:05 ERR [WebRTC] [session 1a2b3c4d] listen udp :0: bind: An attempt was made to "
               "access a socket in a way forbidden by its access permissions.\n"
               + "".join(f"2026/10/08 12:{i:02d}:00 INF [path cam-1/main] [recorder] recording segment\n"
                         for i in range(1, 30))
               + "2026-10-08T10:30:00Z [vmsctl] el proceso terminó con código -1073741819 tras 1800 s\n")
    assert feed(session).exit_error == ""


def test_an_error_written_just_after_the_exit_event_still_counts() -> None:
    """vmsctl copia la salida de MediaMTX desde otro hilo: su última línea puede quedar detrás de «terminó»."""
    late = ("2026-10-08T10:00:00Z [vmsctl] proceso en marcha (pid 300)\n"
            "2026/10/08 12:00:00 INF MediaMTX v1.21.1, windows, amd64\n"
            "2026-10-08T10:00:00Z [vmsctl] el proceso terminó con código 1 tras 0 s\n"
            "2026/10/08 12:00:00 ERR listen tcp 127.0.0.1:9997: bind: Only one usage of each socket address "
            "(protocol/network address/port) is normally permitted.\n"
            "2026/10/08 12:00:00 INF waiting for running hooks\n"
            "2026-10-08T10:00:00Z [vmsctl] lo relanzo dentro de 1 s\n")
    c = feed(late)
    assert c.state == "exited" and ":9997: bind" in c.exit_error
    # …pero no el de un arranque nuevo: ese empieza limpio
    assert feed(late + "2026-10-08T10:00:01Z [vmsctl] proceso en marcha (pid 301)\n").exit_error == c.exit_error
    assert feed(late + "2026-10-08T10:00:01Z [vmsctl] proceso en marcha (pid 301)\n").pid == 301


def test_a_short_run_falls_for_its_first_error() -> None:
    """Un error de arranque que no es «bind» ni «panic», seguido de muchas líneas de cámaras: es la causa."""
    lines = MANY_CAMERAS_BLOCK.replace("listen tcp 127.0.0.1:9997: bind: Only one usage of each socket address "
                                       "(protocol/network address/port) is normally permitted.",
                                       "[WebRTC] invalid ICE server: algo nuevo").splitlines()
    c = feed("\n".join(lines))
    assert c.exit_error == "[WebRTC] invalid ICE server: algo nuevo", c
    # Uno de un arranque largo seguido de horas de líneas de cámaras, no
    long_run = "\n".join(lines).replace("tras 0 s", "tras 7200 s")
    assert feed(long_run).exit_error == ""


def test_engine_error_helper_and_child_mode_reason(tmp_path: Path) -> None:
    assert P.engine_error("2026/10/06 11:06:04 ERR listen tcp 127.0.0.1:9998: bind: x") == \
        "listen tcp 127.0.0.1:9998: bind: x"
    assert P.engine_error("2026/10/06 11:06:04 ERR [path cam/main] [RTSP source] 401") == ""
    assert P.engine_error("2026/10/06 11:06:04 INF ok") == ""
    # Modo child: MediaMTX rechaza el YAML con «ERR: …» sin fecha; antes salía «sin mensaje de error»
    proc = MediaMtxProcess(tmp_path / "mediamtx", tmp_path / "m.yml", workdir=tmp_path, api_host="127.0.0.1",
                           api_port=1)
    proc.recent_lines.extend(["2026/10/07 12:00:00 INF MediaMTX v1.21.1", 'ERR: json: unknown field "x"'])
    assert proc._failure_reason() == 'ERR: json: unknown field "x"'


# ----------------------------------------------------------------------------------------------- archivos
def test_read_run_status_and_log_tail(tmp_path: Path) -> None:
    f = tmp_path / "status-VMSEngine.json"
    assert P.read_run_status(f) == (None, "missing")
    # El de verdad del PC del usuario (beta.2, motor en marcha)
    f.write_text('{"schema": 1, "service": "VMSEngine", "version": "2.0.0-beta.2", "state": "running", "detail": "", '
                 '"vmsctl_pid": 7792, "child_pid": 8480, "started_unix": 1791450636, "crashes_unix": [], '
                 '"updated_unix": 1791460419}', encoding="utf-8")
    run, why = P.read_run_status(f)
    assert why == "" and run is not None and run.state == "running" and run.started_unix == 1791450636
    assert run.child_pid == 8480
    f.write_text("{no es json", encoding="utf-8")
    assert P.read_run_status(f) == (None, "invalid")
    f.write_bytes(b" " * (P.MAX_STATUS_BYTES + 10))
    assert P.read_run_status(f) == (None, "invalid")

    log = tmp_path / "engine.log"
    with open(log, "wb") as fh:
        fh.write(("2026/10/06 11:06:04 INF relleno ñandú\n" * 140_000).encode("utf-8"))   # ~5 MB
        filler = fh.tell()
        fh.write(REAL_CRASH_BLOCK.encode("utf-8"))
    lines = P.read_log_tail(log)
    assert sum(len(ln) + 1 for ln in lines) <= P.LOG_SCAN_BYTES
    assert lines[-1].endswith("lo relanzo dentro de 30 s") and lines[0].startswith("2026/10/06")
    # Hasta donde empieza a seguirlo LogTail
    before = P.read_log_tail(log, end=filler)
    assert before[-1].endswith("relleno ñandú") and sum(len(ln.encode()) + 1 for ln in before) <= P.LOG_SCAN_BYTES
    assert P.read_log_tail(tmp_path / "no-existe.log") == []


# ----------------------------------------------------------------------------------------------- quién tiene el puerto
@pytest.mark.skipif(sys.platform == "darwin", reason="en macOS psutil.net_connections exige ser root")
def test_port_holder_names_the_real_listener() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        port = s.getsockname()[1]
        who = P.port_holder(port, "tcp", "127.0.0.1")
        assert who is not None and who.pid == os.getpid() and who.name.lower().startswith("python"), who
    finally:
        s.close()


def test_port_holder_never_guesses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_psutil(monkeypatch, SYS_ON_9998, {6712: "SYS.exe"})
    assert P.port_holder(9998, "tcp", "127.0.0.1") == P.PortHolder("SYS.exe", 6712)
    # Sin permiso para el nombre del proceso (otra cuenta de Windows): no se dice ningún programa
    fake_psutil(monkeypatch, SYS_ON_9998, {6712: psutil.AccessDenied(6712)})
    assert P.port_holder(9998, "tcp", "127.0.0.1") is None
    fake_psutil(monkeypatch, SYS_ON_9998, {6712: psutil.NoSuchProcess(6712)})
    assert P.port_holder(9998, "tcp", "127.0.0.1") is None
    # Dos programas distintos en el mismo puerto: tampoco
    two = SYS_ON_9998 + [Conn(2, socket.AF_INET6, socket.SOCK_STREAM, Addr("::", 9998), (), psutil.CONN_LISTEN, 77)]
    fake_psutil(monkeypatch, two, {6712: "SYS.exe", 77: "otro.exe"})
    assert P.port_holder(9998, "tcp", "127.0.0.1") is None
    # En otra dirección (la de la LAN) no impide abrir 127.0.0.1
    lan = [Conn(1, socket.AF_INET, socket.SOCK_STREAM, Addr("192.168.1.80", 9998), (), psutil.CONN_LISTEN, 6712)]
    fake_psutil(monkeypatch, lan, {6712: "SYS.exe"})
    assert P.port_holder(9998, "tcp", "127.0.0.1") is None

    def denied(kind: str = "inet") -> list[Conn]:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", denied)
    assert P.port_holder(9998, "tcp", "127.0.0.1") is None


# ----------------------------------------------------------------------------------------------- diagnóstico
def run_status(state: str, now: float, *, crashes: tuple[float, ...] = (), started_ago: float | None = None,
               detail: str = "", updated_ago: float = 1.0) -> P.RunStatus:
    return P.RunStatus(state=state, detail=detail, crashes_unix=crashes, updated_unix=now - updated_ago,
                       started_unix=now - started_ago if started_ago is not None else None)


def diag(run: P.RunStatus | None, cause: P.LogCause, now: float, *, api_status: int | None = None,
         holder: P.HolderLookup = lambda port, proto, host: None) -> EngineProblem:
    p = P.diagnose(api_ok=False, api_status=api_status, run=run, cause=cause, now=now, roles={9997: "control del motor"},
                   holder=holder)
    assert p is not None
    check_texts(p)
    return p


def test_diagnose_the_real_crash_loop_names_the_program() -> None:
    now = LAST_EXIT + 5
    cause = feed(REAL_CRASH_BLOCK)
    run = run_status("backoff", now, crashes=(now - 20, now - 18, now - 16, now - 10, now - 5),
                     detail="el proceso terminó con código 1 tras 0 s")
    p = diag(run, cause, now, holder=lambda port, proto, host: P.PortHolder("SYS.exe", 6712)
             if (port, proto, host) == (9998, "tcp", "127.0.0.1") else None)
    assert p.code == "port_in_use" and "9998" in p.detail_es and "«SYS.exe»" in p.detail_es
    assert "5 veces" in p.detail_es and "SYS.exe" in p.action_es and "9998" in p.action_es
    # Sin saber quién es: no se inventa ningún nombre
    p = diag(run, cause, now)
    assert p.code == "port_in_use" and "otro programa" in p.detail_es and "SYS" not in p.detail_es + p.action_es
    # Sin status-VMSEngine.json (vmsctl antiguo o desarrollo): basta con engine.log
    assert diag(None, cause, now).code == "port_in_use"
    # Durante el instante en que el proceso relanzado «está en marcha» sigue siendo el puerto
    assert diag(run_status("running", now, crashes=(now - 5,), started_ago=0), cause, now).code == "port_in_use"
    # 10013 sin dueño conocido: puede ser otra cuenta o un rango reservado
    forbidden = feed(REAL_CRASH_BLOCK.replace("Only one usage of each socket address (protocol/network address/port) is "
                                              "normally permitted.", "An attempt was made to access a socket in a way "
                                              "forbidden by its access permissions."))
    assert "reservado" in diag(run, forbidden, now).detail_es


def test_diagnose_other_causes() -> None:
    now = time.time()
    none = P.LogCause()
    assert P.diagnose(api_ok=True, api_status=None, run=None, cause=none, now=now) is None
    assert diag(None, none, now).code == "no_api"
    assert diag(None, none, now, api_status=401).code == "api_auth"
    assert diag(run_status("stopped", now), none, now).code == "stopped"
    assert diag(run_status("running", now, started_ago=500, updated_ago=120), none, now).code == "stopped"
    assert diag(run_status("waiting", now, detail="espero a que exista C:\\x\\mediamtx.yml"), none, now).code == \
        "waiting_config"
    assert diag(run_status("failed", now, detail="no existe C:\\x\\mediamtx.exe: la instalación está incompleta",
                           crashes=(now,)), none, now).code == "missing_program"
    assert diag(run_status("running", now, started_ago=5), none, now).code == "starting"
    assert diag(run_status("running", now, started_ago=300), none, now).code == "no_api"
    # Arrancado y estable tras una caída antigua: lo de antes ya no es el motivo
    assert diag(run_status("running", now, started_ago=300, crashes=(now - 400,)), feed(REAL_CRASH_BLOCK),
                now).code == "no_api"

    cfg = diag(run_status("backoff", now, crashes=(now - 2,)), feed(CONFIG_ERROR_BLOCK), now)
    assert cfg.code == "bad_config" and "campoInventado" in cfg.detail_es
    panic = diag(run_status("backoff", now, crashes=(now - 2,)), feed(PANIC_BLOCK), now)
    assert panic.code == "crash_loop" and "panic: runtime error" in panic.detail_es and "playback" not in panic.detail_es
    # Un error desconocido se enseña tal cual, sin credenciales y cortado
    secret = feed("2026-10-07T10:00:00Z [vmsctl] proceso en marcha (pid 1)\n2026/10/07 12:00:00 ERR algo raro con "
                  "rtsp://admin:Cl4ve%21@10.0.0.1:554/x " + "x" * 600 + "\n"
                  "2026-10-07T10:00:01Z [vmsctl] el proceso terminó con código 3 tras 1 s\n")
    p = diag(run_status("backoff", now, crashes=(now - 1,)), secret, now)
    assert p.code == "crash_loop" and "algo raro" in p.detail_es and len(p.detail_es) < 450
    # Sin mensaje de MediaMTX
    assert "ningún mensaje" in diag(run_status("backoff", now, crashes=(now - 1,), detail="el proceso terminó con "
                                               "código 1 tras 0 s"), none, now).detail_es


def test_diagnose_a_taken_port_with_many_cameras_names_port_and_program() -> None:
    now = MANY_EXIT + 5
    run = run_status("backoff", now, crashes=(now - 65, now - 35, now - 5), detail="el proceso terminó con código 1 "
                     "tras 0 s")
    p = diag(run, feed(MANY_CAMERAS_BLOCK), now, holder=lambda port, proto, host: P.PortHolder("javaw.exe", 4242)
             if (port, proto, host) == (9997, "tcp", "127.0.0.1") else None)
    assert p.code == "port_in_use", p
    assert "9997" in p.detail_es and "«javaw.exe»" in p.detail_es and "control del motor" in p.detail_es
    assert diag(run, feed(RELOAD_BLOCK), now).code == "port_in_use"


def test_diagnose_does_not_blame_another_program_without_reason() -> None:
    now = LAST_EXIT + 5
    run = run_status("backoff", now, crashes=(now - 5,), detail="el proceso terminó con código 1 tras 0 s")

    def sys_exe(port: int, proto: str, host: str) -> P.PortHolder | None:
        return P.PortHolder("SYS.exe", 6712)

    def mine(port: int, proto: str, host: str) -> P.PortHolder | None:
        return P.PortHolder("mediamtx.exe", 8480)

    # La dirección del .env ya no es de este equipo (10049): se cita el error, sin culpar a ningún programa
    gone = feed(REAL_CRASH_BLOCK.replace("127.0.0.1:9998: bind: Only one usage of each socket address (protocol/network "
                                         "address/port) is normally permitted.", "192.168.1.80:8554: bind: The "
                                         "requested address is not valid in its context."))
    p = diag(run, gone, now, holder=sys_exe)
    assert p.code == "crash_loop" and "192.168.1.80:8554" in p.detail_es, p
    assert "otro programa" not in p.detail_es and "SYS" not in p.detail_es + p.action_es
    # El puerto ya lo tiene el MediaMTX que vmsctl acaba de relanzar: está arrancando, no es «otro programa»
    relaunched = P.RunStatus(state="running", started_unix=now - 1, crashes_unix=(now - 5,), updated_unix=now - 1,
                             child_pid=8480)
    assert diag(relaunched, feed(REAL_CRASH_BLOCK), now, holder=mine).code == "starting"
    assert diag(relaunched, feed(REAL_CRASH_BLOCK), now, holder=sys_exe).code == "port_in_use"
    # Sin status-VMSEngine.json, el pid sale de «proceso en marcha (pid N)»
    log_only = feed(REAL_CRASH_BLOCK + "2026-10-06T09:06:54Z [vmsctl] proceso en marcha (pid 8480)\n")
    assert diag(None, log_only, LAST_EXIT + 31, holder=mine).code == "starting"
    assert diag(None, log_only, LAST_EXIT + 31, holder=sys_exe).code == "port_in_use"


def test_diagnose_from_engine_log_alone_needs_a_recent_exit() -> None:
    """Sin status-VMSEngine.json (vmsctl antiguo, sin permiso o dañado) solo queda engine.log."""
    # Una caída de hace días no explica que hoy la API no conteste
    assert diag(None, feed(REAL_CRASH_BLOCK), LAST_EXIT + 3 * 86400).code == "no_api"
    # «[vmsctl] parado» es el servicio parado, no la caída de antes
    stopped = feed(REAL_CRASH_BLOCK + "2026-10-06T09:06:54Z [vmsctl] proceso en marcha (pid 20100)\n"
                   "2026-10-06T09:07:20Z [vmsctl] proceso parado de forma ordenada\n"
                   "2026-10-06T09:07:20Z [vmsctl] parado\n")
    assert stopped.state == "stopped" and stopped.pid is None
    assert diag(None, stopped, LAST_EXIT + 60).code == "stopped"


# ----------------------------------------------------------------------------------------------- motor attach
class DownApi:
    """La API de MediaMTX no contesta (motor cayéndose sin parar)."""

    def __init__(self, status: int | None = None) -> None:
        self.status = status

    async def info(self) -> dict[str, Any]:
        if self.status is not None:
            raise MtxApiError(self.status, "unauthorized")
        raise EngineUnavailable("no responde")

    async def aclose(self) -> None:
        return None


def write_status(app_paths: AppPaths, state: str, crashes: int) -> None:
    now = time.time()
    (app_paths.logs_dir / "status-VMSEngine.json").write_text(json.dumps({
        "schema": 1, "service": "VMSEngine", "version": "2.0.0-beta.2", "state": state,
        "detail": "el proceso terminó con código 1 tras 0 s", "vmsctl_pid": 7792, "child_pid": None,
        "started_unix": None, "crashes_unix": [int(now) - 30 * i for i in range(crashes)], "updated_unix": int(now)}),
        encoding="utf-8")


@pytest.fixture
def attach(settings: VmsSettings, app_paths: AppPaths, monkeypatch: pytest.MonkeyPatch) -> MediaMtxEngine:
    import vms.engine.engine as engmod

    monkeypatch.setattr(engmod, "MediaMtxApi", lambda *a, **k: DownApi())
    monkeypatch.setattr(engmod, "DIAG_CACHE_S", 0.0)
    return MediaMtxEngine(settings, app_paths, mode="attach", disk_guard_interval=3600, attach_poll=0.05,
                          logtail_interval=0.05)


async def test_status_explains_the_real_9998_crash_loop(attach: MediaMtxEngine, app_paths: AppPaths,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """El caso del usuario: el backend (servicio de inicio retrasado) arranca con el motor ya en bucle de caídas."""
    (app_paths.logs_dir / "engine.log").write_text(REAL_CRASH_BLOCK, encoding="utf-8")
    write_status(app_paths, "backoff", crashes=18)
    fake_psutil(monkeypatch, SYS_ON_9998, {6712: "SYS.exe"})
    await attach.start()
    try:
        st = await attach.status()
        assert not st.running and st.problem is not None, st
        assert st.problem.code == "port_in_use"
        assert "9998" in st.problem.detail_es and "SYS.exe" in st.problem.detail_es
        assert st.service_state == "backoff" and st.crashes_10min == 18
        assert "Qué hacer:" in st.last_error and "vmsctl services status" not in st.last_error
        check_texts(st.problem)
        # Si no se puede saber el programa (otra cuenta sin permiso), no se nombra ninguno
        fake_psutil(monkeypatch, SYS_ON_9998, {6712: psutil.AccessDenied(6712)})
        attach._holders.clear()
        st = await attach.status()
        assert st.problem is not None and st.problem.code == "port_in_use"
        assert "otro programa" in st.problem.detail_es and "SYS" not in st.last_error
    finally:
        await attach.stop()


async def wait_problem(engine: MediaMtxEngine, code: str) -> EngineProblem:
    for _ in range(100):
        st = await engine.status()
        if st.problem is not None and st.problem.code == code:
            return st.problem
        await asyncio.sleep(0.05)
    raise AssertionError(f"no llegó {code}: {st.problem}")


async def test_status_follows_engine_log_while_running(attach: MediaMtxEngine, app_paths: AppPaths,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Con el backend ya en marcha, lo que va escribiendo vmsctl en engine.log (LogTail)."""
    log = app_paths.logs_dir / "engine.log"
    log.write_text("", encoding="utf-8")
    fake_psutil(monkeypatch, SYS_ON_9998, {6712: "SYS.exe"})
    await attach.start()
    try:
        assert (await attach.status()).problem.code == "no_api"   # type: ignore[union-attr]
        write_status(app_paths, "backoff", crashes=1)
        with open(log, "a", encoding="utf-8") as f:
            f.write(CONFIG_ERROR_BLOCK)
        p = await wait_problem(attach, "bad_config")
        assert "campoInventado" in p.detail_es
        with open(log, "a", encoding="utf-8") as f:
            f.write(PANIC_BLOCK)
        p = await wait_problem(attach, "crash_loop")
        assert "panic:" in p.detail_es and "[playback]" not in p.detail_es
        with open(log, "a", encoding="utf-8") as f:
            f.write(REAL_CRASH_BLOCK)
        p = await wait_problem(attach, "port_in_use")
        assert "9998" in p.detail_es and "SYS.exe" in p.detail_es
    finally:
        await attach.stop()


async def test_status_names_a_taken_port_with_many_cameras(attach: MediaMtxEngine, app_paths: AppPaths,
                                                           settings: VmsSettings,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Hallazgo de la revisión: con 4 cámaras o más, el «bind» de RTSP, WebRTC o la API quedaba lejos de la salida y
    Estado del sistema decía «se ha cerrado por un error… no ha dejado ningún mensaje», sin puerto ni programa."""
    api = settings.mtx_api_address
    port = int(api.rsplit(":", 1)[1])
    block = MANY_CAMERAS_BLOCK.replace("127.0.0.1:9997", api)
    fake_psutil(monkeypatch, [Conn(1, socket.AF_INET, socket.SOCK_STREAM, Addr("127.0.0.1", port), (),
                                   psutil.CONN_LISTEN, 4242)], {4242: "javaw.exe"})
    log = app_paths.logs_dir / "engine.log"
    # El backend arranca con el motor ya cayéndose (lee el final de engine.log)…
    log.write_text(block, encoding="utf-8")
    write_status(app_paths, "backoff", crashes=3)
    await attach.start()
    try:
        st = await attach.status()
        assert st.problem is not None and st.problem.code == "port_in_use", st
        assert str(port) in st.problem.detail_es and "«javaw.exe»" in st.problem.detail_es
        assert "control del motor" in st.problem.detail_es and "Qué hacer:" in st.last_error
        check_texts(st.problem)
        # …y con el backend ya en marcha (LogTail), tras otra caída distinta
        with open(log, "a", encoding="utf-8") as f:
            f.write(CONFIG_ERROR_BLOCK)
        await wait_problem(attach, "bad_config")
        with open(log, "a", encoding="utf-8") as f:
            f.write(block)
        p = await wait_problem(attach, "port_in_use")
        assert str(port) in p.detail_es and "«javaw.exe»" in p.detail_es
    finally:
        await attach.stop()


async def test_lines_written_while_the_backend_starts_are_not_lost(attach: MediaMtxEngine, app_paths: AppPaths,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Entre leer el final de engine.log y la primera vuelta de LogTail (que empieza por el final), vmsctl puede
    escribir. Antes esas líneas no las leía nadie hasta la siguiente caída."""
    log = app_paths.logs_dir / "engine.log"
    log.write_text("2026-10-07T09:59:59Z [vmsctl] VMSEngine: arranco C:\\x\\mediamtx.exe (pid 1)\n", encoding="utf-8")
    write_status(app_paths, "backoff", crashes=1)
    real_tail = P.read_log_tail

    def tail_then_vmsctl_writes(path: Path, max_bytes: int = P.LOG_SCAN_BYTES, *,
                                end: int | None = None) -> list[str]:
        lines = real_tail(path, max_bytes) if end is None else real_tail(path, max_bytes, end=end)
        with open(log, "a", encoding="utf-8") as f:
            f.write(CONFIG_ERROR_BLOCK)
        return lines

    monkeypatch.setattr(P, "read_log_tail", tail_then_vmsctl_writes)
    await attach.start()
    try:
        p = await wait_problem(attach, "bad_config")
        assert "campoInventado" in p.detail_es
    finally:
        await attach.stop()


async def test_a_broken_diagnosis_never_breaks_the_engine(attach: MediaMtxEngine, app_paths: AppPaths,
                                                          monkeypatch: pytest.MonkeyPatch,
                                                          caplog: pytest.LogCaptureFixture) -> None:
    """Explicar la caída es un extra: un fallo inesperado ahí no puede impedir arrancar ni dejar sin estado."""
    (app_paths.logs_dir / "engine.log").write_text(REAL_CRASH_BLOCK, encoding="utf-8")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fallo inesperado")

    monkeypatch.setattr(P, "read_log_tail", boom)
    monkeypatch.setattr(P, "diagnose", boom)
    with caplog.at_level(logging.ERROR, logger="vms.engine"):
        await attach.start()
        try:
            for _ in range(3):
                st = await attach.status()
                assert not st.running and st.problem is None, st
        finally:
            await attach.stop()
    assert len([r for r in caplog.records if "por qué no responde" in r.getMessage()]) == 1


async def test_problem_disappears_when_the_api_answers(attach: MediaMtxEngine, app_paths: AppPaths) -> None:
    (app_paths.logs_dir / "engine.log").write_text(REAL_CRASH_BLOCK, encoding="utf-8")
    write_status(app_paths, "running", crashes=3)

    class UpApi(DownApi):
        async def info(self) -> dict[str, Any]:
            return {"version": "v1.21.1", "started": "2026-10-08T10:00:00Z"}

    await attach.start()
    try:
        assert (await attach.status()).problem is not None
        attach.api = UpApi()   # type: ignore[assignment]
        st = await attach.status()
        assert st.running and st.problem is None and st.last_error == "" and st.crashes_10min == 3
    finally:
        await attach.stop()


async def test_internal_credentials_rejected(settings: VmsSettings, app_paths: AppPaths,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    import vms.engine.engine as engmod

    monkeypatch.setattr(engmod, "MediaMtxApi", lambda *a, **k: DownApi(status=401))
    eng = MediaMtxEngine(settings, app_paths, mode="attach", disk_guard_interval=3600, attach_poll=0.05)
    await eng.start()
    try:
        st = await eng.status()
        assert st.problem is not None and st.problem.code == "api_auth"
    finally:
        await eng.stop()


def test_the_same_bind_error_is_logged_once(attach: MediaMtxEngine, caplog: pytest.LogCaptureFixture) -> None:
    """Antes, el mismo «listen tcp …: bind» llegaba a vms.log en cada relanzamiento (1568 veces en el PC del
    usuario). Ahora una vez cada REPEAT_SILENCE_S; el motivo está en Estado del sistema."""
    line = ("2026/10/06 11:06:{:02d} ERR listen tcp 127.0.0.1:9998: bind: Only one usage of each socket address "
            "(protocol/network address/port) is normally permitted.")
    with caplog.at_level(logging.INFO, logger="vms.engine.mediamtx"):
        for i in range(5):
            attach._on_line(line.format(i), relay=False)
        attach._on_line("2026/10/06 11:07:00 ERR otro error distinto", relay=False)
    errors = [r.getMessage() for r in caplog.records if r.name == "vms.engine.mediamtx" and r.levelno >= logging.WARNING]
    assert len([e for e in errors if "9998" in e]) == 1 and "otro error distinto" in errors


def test_engine_errors_do_not_reset_the_silence_of_camera_errors(attach: MediaMtxEngine,
                                                                 caplog: pytest.LogCaptureFixture) -> None:
    """Los errores del propio MediaMTX pueden llevar el identificador de cada conexión (todos distintos). Si llenan
    la misma tabla que los de las cámaras, al vaciarla cada error repetido de una cámara vuelve a vms.log."""
    camera = "2026/10/06 11:06:00 ERR [path cam-1/main] [RTSP source] dial tcp 192.0.2.10:554: i/o timeout"
    with caplog.at_level(logging.INFO, logger="vms.engine.mediamtx"):
        attach._on_line(camera, relay=False)
        for i in range(2100):
            attach._on_line(f"2026/10/06 11:06:01 WAR [RTSP] [conn 127.0.0.1:{10000 + i}] algo raro", relay=False)
        caplog.clear()
        attach._on_line(camera, relay=False)
    assert not [r for r in caplog.records if "cam-1" in r.getMessage()], "el mismo error de la cámara, otra vez"


def test_logtail_warns_once_when_engine_log_is_not_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    log = tmp_path / "engine.log"
    log.write_text("a\n", encoding="utf-8")
    t = LogTail(log, lambda line: None)
    t.poll()
    with open(log, "a", encoding="utf-8") as f:
        f.write("b\n")

    def denied(self: LogTail, path: Path, offset: int) -> tuple[bytes, int]:
        raise PermissionError(13, "Acceso denegado")

    monkeypatch.setattr(LogTail, "_read_from", denied)
    with caplog.at_level(logging.DEBUG, logger="vms.engine.logtail"):
        for _ in range(3):
            t.poll()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Qué hacer:" in warnings[0].getMessage()
