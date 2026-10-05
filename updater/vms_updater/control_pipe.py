"""Control local del actualizador (CONTRATO §15.2): tubería `\\\\.\\pipe\\VMSMultimarca.updater`.

- **ACL:** SYSTEM y Administradores (todo) y, solo para entregar la directiva del panel central, las cuentas
  de servicio que mandan el latido (`NT SERVICE\\VMSHeartbeat` y `NT SERVICE\\VMSBackend`), con los derechos
  justos de cliente (sin `FILE_CREATE_PIPE_INSTANCE`: no pueden crear instancias del servidor). Un
  administrador en una sesión normal tiene el token filtrado por UAC, así que tiene que **elevar**. Además
  `PIPE_REJECT_REMOTE_CLIENTS`: nada desde la red. No hay TCP.
- **Quién llama:** el servidor identifica la cuenta del cliente (el cliente abre con `SECURITY_IDENTIFICATION`)
  y las cuentas de latido solo pueden usar `status` y `directive` (`DIRECTIVE_ONLY`).
- **Suplantación:** la primera instancia se crea con `FILE_FLAG_FIRST_PIPE_INSTANCE`; si otro proceso ya tiene
  el nombre, se reintenta y el problema se ve en `public-status.json` (nunca se queda callado). El cliente
  (`pipe_client.py`) comprueba que al otro lado está SYSTEM antes de enviar nada.
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

from pydantic import ValidationError

from ._atomic import atomic_write_json
from .engine import Engine
from .models import CentralDirective
from .pipe_client import CLIENT_ACCESS, request as client_request, service_sid
from .state_files import read_local_config, write_local_config

log = logging.getLogger("vms_updater.pipe")

PIPE_NAME = r"\\.\pipe\VMSMultimarca.updater"
# Servicios que entregan la directiva del panel central (latido por agente HTTP o directo desde el backend)
HEARTBEAT_SERVICES = ("VMSHeartbeat", "VMSBackend")
DIRECTIVE_ONLY = frozenset({"status", "directive"})
PIPE_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)" + "".join(f"(A;;0x{CLIENT_ACCESS:x};;;{service_sid(s)})"
                                                   for s in HEARTBEAT_SERVICES)
MAX_MESSAGE = 64 * 1024
FIRST_INSTANCE_RETRY_S = 5.0

Caller = str   # «admin» (SYSTEM, Administradores, socket Unix 0600) o «heartbeat» (solo DIRECTIVE_ONLY)


# --------------------------------------------------------------------------- protocolo
def handle_request(engine: Engine, req: Any, caller: Caller = "admin") -> dict[str, Any]:
    if not isinstance(req, dict):
        return {"ok": False, "error": "bad_request", "message_es": "La petición tiene que ser un objeto JSON"}
    cmd = req.get("cmd")
    if caller != "admin" and cmd not in DIRECTIVE_ONLY:
        log.warning("Orden «%s» rechazada: la cuenta del latido solo puede entregar la directiva", cmd)
        return {"ok": False, "error": "forbidden", "message_es": "Esta cuenta solo puede entregar la directiva"}
    try:
        if cmd == "directive":
            raw = req.get("directive")
            if raw is None:
                engine.layout.directive_file.unlink(missing_ok=True)   # el panel ya no pide nada
                return {"ok": True}
            try:
                d = CentralDirective.model_validate(raw)
            except ValidationError:
                return {"ok": False, "error": "bad_request", "message_es": "Directiva del panel no válida"}
            if d.rollback_to not in (None, "previous"):   # «previous» = la anterior (el panel no la conoce)
                from .versioning import Version
                try:
                    Version.parse(str(d.rollback_to))
                except ValueError:
                    return {"ok": False, "error": "bad_request", "message_es": "Versión no válida en la directiva"}
            atomic_write_json(engine.layout.directive_file, d.model_dump(mode="json"))
            return {"ok": True}
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
        if cmd == "unskip":
            version = req.get("version")
            if version is not None and not isinstance(version, str):
                return {"ok": False, "error": "bad_request", "message_es": "«version» tiene que ser texto"}
            gone = engine.unskip(version)
            return {"ok": True, "unskipped": gone, "skipped": engine.blacklist.skipped()}
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
            # No se concede mientras se esté APLICANDO algo (pasos del diario, vuelta atrás, ranura del
            # actualizador); sí durante una comprobación o descarga: antes de aplicar, la comprobación mira el
            # cerrojo con `_apply` cogido y espera. No se espera a que acabe: «busy» al momento.
            if not engine._apply.acquire(blocking=False):
                return {"ok": False, "error": "busy", "message_es": "Hay una actualización aplicándose"}
            try:
                ok = engine.lock.acquire(owner, ttl)
            finally:
                engine._apply.release()
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
    """Socket 0600 del propio usuario del actualizador: quien conecta es «admin» (solo desarrollo)."""

    def __init__(self, path: Path, handler: Callable[[Any, Caller], dict[str, Any]]) -> None:
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
            # Una conexión por hilo: un «status» no espera a que termine un «check» largo
            threading.Thread(target=self._serve, args=(conn,), name="updater-control-conn", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            data = b""
            try:
                while b"\n" not in data and len(data) < MAX_MESSAGE:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                conn.settimeout(None)
                conn.sendall(_encode(self.handler(_decode(data), "admin")))
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


# --------------------------------------------------------------------------- servidor Windows
class WindowsPipeServer:  # pragma: no cover - solo Windows (job B4 de CI)
    def __init__(self, handler: Callable[[Any, Caller], dict[str, Any]], name: str = PIPE_NAME,
                 sddl: str = PIPE_SDDL, on_problem: Callable[[str], None] | None = None,
                 restricted_sids: frozenset[str] | None = None) -> None:
        self.handler = handler
        self.name = name
        self.sddl = sddl
        self.on_problem = on_problem or (lambda msg: None)
        self.restricted_sids = restricted_sids if restricted_sids is not None else \
            frozenset(service_sid(s) for s in HEARTBEAT_SERVICES)
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
                err = ctypes.get_last_error()
                # 5 (acceso denegado) o 231 (ocupada) con la primera instancia: OTRO proceso tiene el nombre.
                # Se reintenta y se deja visible; nunca se renuncia en silencio.
                msg = (f"No se pudo abrir el control del actualizador (error {err}): otro programa ocupa su "
                       "tubería. Reinicia el equipo; si sigue, avisa a soporte." if first else
                       f"No se pudo abrir una conexión de control del actualizador (error {err})")
                log.error(msg)
                self.on_problem(msg)
                self._stop.wait(FIRST_INSTANCE_RETRY_S)
                continue
            if first:
                self.on_problem("")
            first = False
            ok = connect(h, None) or ctypes.get_last_error() == 535   # ERROR_PIPE_CONNECTED
            if not ok or self._stop.is_set():
                k32.CloseHandle(h)
                continue

            def serve(h: Any = h) -> None:
                try:
                    buf = ctypes.create_string_buffer(MAX_MESSAGE)
                    n = wintypes.DWORD()
                    data = b""
                    while b"\n" not in data and len(data) < MAX_MESSAGE:
                        if not read(h, buf, MAX_MESSAGE, ctypes.byref(n), None) or n.value == 0:
                            break
                        data += buf.raw[: n.value]
                    try:
                        caller: Caller = "heartbeat" if client_sid(h) in self.restricted_sids else "admin"
                    except OSError as exc:
                        log.warning("No se pudo identificar al cliente de la tubería: %s", exc)
                        caller = "heartbeat"         # en la duda, lo mínimo
                    out = _encode(self.handler(_decode(data), caller))
                    written = wintypes.DWORD()
                    write(h, out, len(out), ctypes.byref(written), None)
                    k32.FlushFileBuffers(h)
                    k32.DisconnectNamedPipe(h)
                finally:
                    k32.CloseHandle(h)

            # Una conexión por hilo (la siguiente instancia de la tubería se crea en seguida)
            threading.Thread(target=serve, name="updater-pipe-conn", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:   # desbloquea ConnectNamedPipe
            with open(self.name, "r+b", buffering=0):
                pass
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)


def client_sid(h: Any) -> str:  # pragma: no cover - Windows
    """Cuenta del cliente conectado a la instancia `h` (se suplanta un momento solo para leer su token)."""
    if sys.platform != "win32":
        raise OSError("Solo en Windows")
    import ctypes
    from ctypes import wintypes

    from .pipe_client import token_user_sid, win_api

    k32, adv = win_api()
    adv.ImpersonateNamedPipeClient.argtypes = [wintypes.HANDLE]
    adv.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
    k32.GetCurrentThread.restype = wintypes.HANDLE
    if not adv.ImpersonateNamedPipeClient(h):
        raise ctypes.WinError(ctypes.get_last_error())
    tok = wintypes.HANDLE()
    try:
        if not adv.OpenThreadToken(k32.GetCurrentThread(), 0x0008, True, ctypes.byref(tok)):   # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        adv.RevertToSelf()
    try:
        return token_user_sid(tok)
    finally:
        k32.CloseHandle(tok)


def make_server(engine: Engine) -> UnixControlServer | WindowsPipeServer:
    def handler(req: Any, caller: Caller) -> dict[str, Any]:
        return handle_request(engine, req, caller)

    def problem(msg: str) -> None:
        try:
            engine.status.update(control_error=msg or None)
        except Exception:  # noqa: BLE001 - el estado público no puede tumbar el control
            log.debug("No se pudo anotar el problema de la tubería", exc_info=True)

    if sys.platform == "win32":
        return WindowsPipeServer(handler, on_problem=problem)
    return UnixControlServer(engine.layout.updater_data / "control.sock", handler)


def request(layout_updater_data: Path, req: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
    """Cliente: en Windows comprueba que el servidor es el actualizador (SYSTEM) antes de enviar nada."""
    return client_request(layout_updater_data, req, timeout)
