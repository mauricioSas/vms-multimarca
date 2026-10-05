"""Control local del actualizador (CONTRATO §15.2): tubería `\\\\.\\pipe\\VMSMultimarca.updater`.

- **ACL:** solo SYSTEM y Administradores (`D:P(A;;GA;;;SY)(A;;GA;;;BA)`). Un administrador en una sesión
  normal tiene el token filtrado por UAC (Administradores como «solo denegar»), así que tiene que **elevar**.
  Además `PIPE_REJECT_REMOTE_CLIENTS`: nada desde la red. No hay TCP.
- Mensajes JSON de una línea (UTF-8), una petición y una respuesta por conexión.
- En macOS/Linux (desarrollo y pruebas) el mismo protocolo va por un socket Unix `updater/control.sock`
  con permisos 0600.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .engine import Engine
from .state_files import read_local_config, write_local_config

log = logging.getLogger("vms_updater.pipe")

PIPE_NAME = r"\\.\pipe\VMSMultimarca.updater"
PIPE_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)"
MAX_MESSAGE = 64 * 1024


# --------------------------------------------------------------------------- protocolo
def handle_request(engine: Engine, req: Any) -> dict[str, Any]:
    if not isinstance(req, dict):
        return {"ok": False, "error": "bad_request", "message_es": "La petición tiene que ser un objeto JSON"}
    cmd = req.get("cmd")
    try:
        if cmd == "status":
            st = engine.status.read().dump()
            j = engine.journal.read()
            return {"ok": True, **st, "journal": j.dump() if j else None}
        if cmd == "check":
            out = engine.check(force_window=bool(req.get("force_window")), apply=req.get("apply", True) is not False)
            ok = out.result not in ("error", "update_failed", "metadata_expired", "clock_skew")
            return {"ok": ok, "found": out.available or out.applied, "result": out.result,
                    "message_es": out.message_es}
        if cmd == "rollback":
            to = req.get("to")
            if to is not None and not isinstance(to, str):
                return {"ok": False, "error": "bad_request", "message_es": "«to» tiene que ser texto"}
            out = engine.manual_rollback(to, str(req.get("reason") or "")[:300])
            if out.result != "rollback_ok":
                return {"ok": False, "error": "rollback_failed", "message_es": out.message_es}
            j = engine.journal.read()
            return {"ok": True, "update_id": j.update_id if j else ""}
        if cmd == "hold":
            on = req.get("on")
            if not isinstance(on, bool):
                return {"ok": False, "error": "bad_request", "message_es": "«on» tiene que ser true o false"}
            cfg = read_local_config(engine.layout.local_config_file)
            write_local_config(engine.layout.local_config_file, cfg.model_copy(update={"hold": on}))
            engine.status.update(hold=on)
            return {"ok": True}
        if cmd == "lock":
            owner = str(req.get("owner") or "")[:64]
            ttl = int(req.get("ttl_s") or 3600)
            if not owner:
                return {"ok": False, "error": "bad_request", "message_es": "Falta «owner»"}
            with engine._op:   # no se concede mientras haya una actualización en curso
                ok = engine.lock.acquire(owner, ttl)
            return {"ok": True} if ok else {"ok": False, "error": "busy"}
        if cmd == "unlock":
            owner = str(req.get("owner") or "")[:64]
            return {"ok": True} if engine.lock.release(owner) else {"ok": False, "error": "busy"}
    except Exception as exc:  # noqa: BLE001 - la tubería responde siempre
        log.exception("Error atendiendo «%s»", cmd)
        return {"ok": False, "error": "internal", "message_es": f"Error interno: {type(exc).__name__}"}
    return {"ok": False, "error": "unknown_command", "message_es": f"Orden desconocida: {cmd!r}"}


def _decode(raw: bytes) -> Any:
    line = raw.split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _encode(resp: dict[str, Any]) -> bytes:
    return (json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- servidor POSIX (desarrollo)
class UnixControlServer:
    def __init__(self, path: Path, handler: Callable[[Any], dict[str, Any]]) -> None:
        self.path = Path(path)
        self.handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            s.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        s.listen(4)
        s.settimeout(0.5)
        self._sock = s
        self._thread = threading.Thread(target=self._loop, name="updater-control", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(10)
                data = b""
                try:
                    while b"\n" not in data and len(data) < MAX_MESSAGE:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    conn.sendall(_encode(self.handler(_decode(data))))
                except OSError as exc:
                    log.debug("Conexión de control cortada: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        try:
            self.path.unlink()
        except OSError:
            pass


def unix_request(path: Path, req: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(_encode(req))
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    resp = _decode(data)
    return resp if isinstance(resp, dict) else {"ok": False, "error": "bad_response"}


# --------------------------------------------------------------------------- servidor Windows
class WindowsPipeServer:  # pragma: no cover - solo Windows (job B4 de CI)
    def __init__(self, handler: Callable[[Any], dict[str, Any]], name: str = PIPE_NAME,
                 sddl: str = PIPE_SDDL) -> None:
        self.handler = handler
        self.name = name
        self.sddl = sddl
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="updater-pipe", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        sd = ctypes.c_void_p()
        conv = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
        conv.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        conv.restype = wintypes.BOOL
        if not conv(self.sddl, 1, ctypes.byref(sd), None):
            log.error("No se pudo crear el descriptor de seguridad de la tubería (%d)", ctypes.get_last_error())
            return
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
        create = k32.CreateNamedPipeW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                           wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)]
        create.restype = wintypes.HANDLE
        connect = k32.ConnectNamedPipe
        connect.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        read = k32.ReadFile
        read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        write = k32.WriteFile
        write.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        invalid = wintypes.HANDLE(-1).value
        first = True
        while not self._stop.is_set():
            # PIPE_ACCESS_DUPLEX (3) [+ FILE_FLAG_FIRST_PIPE_INSTANCE 0x80000 la primera vez: nadie puede
            # adelantarse a crear la tubería con otra ACL]; PIPE_TYPE_BYTE|READMODE_BYTE|WAIT|REJECT_REMOTE (8)
            h = create(self.name, 3 | (0x00080000 if first else 0), 0x8, 255, MAX_MESSAGE, MAX_MESSAGE, 0,
                       ctypes.byref(sa))
            if h == invalid or h is None:
                log.error("CreateNamedPipeW falló (%d)", ctypes.get_last_error())
                return
            first = False
            try:
                ok = connect(h, None) or ctypes.get_last_error() == 535   # ERROR_PIPE_CONNECTED
                if not ok or self._stop.is_set():
                    continue
                buf = ctypes.create_string_buffer(MAX_MESSAGE)
                n = wintypes.DWORD()
                data = b""
                while b"\n" not in data and len(data) < MAX_MESSAGE:
                    if not read(h, buf, MAX_MESSAGE, ctypes.byref(n), None) or n.value == 0:
                        break
                    data += buf.raw[: n.value]
                out = _encode(self.handler(_decode(data)))
                written = wintypes.DWORD()
                write(h, out, len(out), ctypes.byref(written), None)
                k32.FlushFileBuffers(h)
                k32.DisconnectNamedPipe(h)
            finally:
                k32.CloseHandle(h)

    def stop(self) -> None:
        self._stop.set()
        try:   # desbloquea ConnectNamedPipe
            with open(self.name, "r+b", buffering=0):
                pass
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)


def pipe_request(req: dict[str, Any], name: str = PIPE_NAME) -> dict[str, Any]:  # pragma: no cover - Windows
    with open(name, "r+b", buffering=0) as f:
        f.write(_encode(req))
        data = b""
        while b"\n" not in data:
            chunk = f.read(65536)
            if not chunk:
                break
            data += chunk
    resp = _decode(data)
    return resp if isinstance(resp, dict) else {"ok": False, "error": "bad_response"}


def make_server(engine: Engine) -> UnixControlServer | WindowsPipeServer:
    def handler(req: Any) -> dict[str, Any]:
        return handle_request(engine, req)

    if sys.platform == "win32":
        return WindowsPipeServer(handler)
    return UnixControlServer(engine.layout.updater_data / "control.sock", handler)


def request(layout_updater_data: Path, req: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
    if sys.platform == "win32":
        return pipe_request(req)
    return unix_request(layout_updater_data / "control.sock", req, timeout)
