"""Cliente de la tubería de control del actualizador (CONTRATO §15.2), sin dependencias fuera de la biblioteca
estándar.

Este archivo existe dos veces, idéntico byte a byte: `updater/vms_updater/pipe_client.py` (runtime del
actualizador) y `central/updater_pipe_client.py` (runtime de la app: el latido entrega por aquí las directivas
del panel central). `tests/updater/test_control_security.py` comprueba que son iguales: se edita uno y se copia.

Defensas contra la suplantación del servidor (hallazgo «Seguridad ALTO 2»):
- Se abre con `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION`: un servidor falso que se quede con el nombre
  solo puede **identificar** al cliente, nunca hacerse pasar por él (sin eso, con el token de un administrador).
- Antes de escribir nada se comprueba **quién** es el servidor: el dueño del objeto tubería tiene que ser SYSTEM
  o Administradores (un servicio con cuenta virtual no puede poner esos dueños) y, si el proceso servidor se
  puede abrir (`GetNamedPipeServerProcessId`), su token tiene que ser de SYSTEM (`VMSUpdater` es LocalSystem).
- Se pide solo `CLIENT_ACCESS`: leer, escribir, atributos y descriptor. Nunca `FILE_CREATE_PIPE_INSTANCE`.
"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

PIPE_NAME = r"\\.\pipe\VMSMultimarca.updater"
MAX_MESSAGE = 64 * 1024
SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"
# FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE. Sin FILE_APPEND_DATA
# (0x4 = FILE_CREATE_PIPE_INSTANCE en una tubería): quien lo tenga podría crear instancias del servidor.
CLIENT_ACCESS = 0x0001 | 0x0002 | 0x0080 | 0x00020000 | 0x00100000


class ServerNotTrusted(PermissionError):
    """Al otro lado de la tubería no está el actualizador (otro proceso se quedó con el nombre)."""


def service_sid(name: str) -> str:
    """SID de servicio `NT SERVICE\\<name>` (S-1-5-80-…): SHA-1 del nombre en mayúsculas en UTF-16LE, leído como
    cinco enteros de 32 bits little-endian. El mismo cálculo que `vms_common::sid::service_sid` (Rust)."""
    d = hashlib.sha1(name.upper().encode("utf-16-le")).digest()  # noqa: S324 - derivación de Windows, no seguridad
    return "S-1-5-80-" + "-".join(str(int.from_bytes(d[i:i + 4], "little")) for i in range(0, 20, 4))


def encode(req: dict[str, Any]) -> bytes:
    return (json.dumps(req, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(raw: bytes) -> Any:
    line = raw.split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def unix_request(path: Path, req: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(encode(req))
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    resp = decode(data)
    return resp if isinstance(resp, dict) else {"ok": False, "error": "bad_response"}


def request(updater_data: Path, req: dict[str, Any], timeout: float = 600.0) -> dict[str, Any]:
    """Windows: la tubería con nombre. macOS/Linux (desarrollo): `<datos>/updater/control.sock` (0600)."""
    if sys.platform == "win32":
        return pipe_request(req, timeout=timeout)
    return unix_request(Path(updater_data) / "control.sock", req, timeout)


# --------------------------------------------------------------------------- Windows
def win_api() -> tuple[Any, Any]:  # pragma: no cover - Windows
    """kernel32 y advapi32 con los tipos de cada función declarados (un HANDLE no cabe en un `int` de C)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    H, D, P = wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, D, D, P, D, D, H]
    k32.CreateFileW.restype = H
    k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, D]
    k32.ReadFile.argtypes = [H, P, D, ctypes.POINTER(D), P]
    k32.WriteFile.argtypes = [H, P, D, ctypes.POINTER(D), P]
    k32.CloseHandle.argtypes = [H]
    k32.LocalFree.argtypes = [P]
    k32.LocalFree.restype = P
    k32.GetNamedPipeServerProcessId.argtypes = [H, ctypes.POINTER(wintypes.ULONG)]
    k32.OpenProcess.argtypes = [D, wintypes.BOOL, D]
    k32.OpenProcess.restype = H
    adv.ConvertSidToStringSidW.argtypes = [P, ctypes.POINTER(P)]
    adv.GetTokenInformation.argtypes = [H, ctypes.c_int, P, D, ctypes.POINTER(D)]
    adv.OpenProcessToken.argtypes = [H, D, ctypes.POINTER(H)]
    adv.GetSecurityInfo.argtypes = [H, ctypes.c_int, D, ctypes.POINTER(P), P, P, P, ctypes.POINTER(P)]
    adv.GetSecurityInfo.restype = D
    return k32, adv


def pipe_request(req: dict[str, Any], name: str = PIPE_NAME, *, timeout: float = 600.0,
                 trusted_sids: frozenset[str] | None = None) -> dict[str, Any]:  # pragma: no cover - Windows
    """Una petición y su respuesta. `trusted_sids`: cuentas que pueden ser el servidor (por defecto SYSTEM; las
    pruebas pasan la suya). Lanza `ServerNotTrusted` si el servidor no lo es, `FileNotFoundError` si no hay
    tubería y `PermissionError` si su ACL no deja entrar."""
    import ctypes
    from ctypes import wintypes

    k32, _ = win_api()
    invalid = wintypes.HANDLE(-1).value
    # OPEN_EXISTING (3); SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION (SecurityIdentification << 16)
    flags = 0x00100000 | 0x00010000
    deadline = time.monotonic() + min(timeout, 30.0)
    while True:
        h = k32.CreateFileW(name, CLIENT_ACCESS, 0, None, 3, flags, None)
        if h not in (invalid, None):
            break
        err = ctypes.get_last_error()
        if err == 231 and time.monotonic() < deadline:          # ERROR_PIPE_BUSY: todas las instancias ocupadas
            k32.WaitNamedPipeW(name, 2000)
            continue
        raise ctypes.WinError(err)
    data = b""
    try:
        check_server(h, trusted_sids or frozenset({SYSTEM_SID}))
        out = encode(req)
        n = wintypes.DWORD()
        if not k32.WriteFile(h, out, len(out), ctypes.byref(n), None):
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(65536)
        while b"\n" not in data:
            if not k32.ReadFile(h, buf, len(buf), ctypes.byref(n), None) or n.value == 0:
                break
            data += buf.raw[: n.value]
    finally:
        k32.CloseHandle(h)
    resp = decode(data)
    return resp if isinstance(resp, dict) else {"ok": False, "error": "bad_response"}


def sid_text(psid: Any) -> str:  # pragma: no cover - Windows
    import ctypes

    k32, adv = win_api()
    out = ctypes.c_void_p()
    if not adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return str(ctypes.wstring_at(out.value))
    finally:
        k32.LocalFree(out)


def token_info(token: Any, klass: int) -> Any:  # pragma: no cover - Windows
    """Búfer con `GetTokenInformation(token, klass)` (1 = TokenUser, 2 = TokenGroups)."""
    import ctypes
    from ctypes import wintypes

    _, adv = win_api()
    size = wintypes.DWORD()
    adv.GetTokenInformation(token, klass, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(max(size.value, 64))
    if not adv.GetTokenInformation(token, klass, buf, len(buf), ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf


def token_user_sid(token: Any) -> str:  # pragma: no cover - Windows
    import ctypes

    buf = token_info(token, 1)            # TOKEN_USER = {SID_AND_ATTRIBUTES User}: el primer campo es el PSID
    return sid_text(ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0])


def check_server(h: Any, trusted: frozenset[str]) -> None:  # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    k32, adv = win_api()
    # 1) Dueño del objeto tubería (lo fija quien la creó; un servicio sin privilegios no puede poner SYSTEM ni
    #    Administradores como dueño).
    owner = ctypes.c_void_p()
    psd = ctypes.c_void_p()
    rc = adv.GetSecurityInfo(h, 6, 1, ctypes.byref(owner), None, None, None, ctypes.byref(psd))  # SE_KERNEL_OBJECT
    if rc != 0:
        raise ServerNotTrusted(f"No se pudo leer el dueño de la tubería (error {rc}): no se envía nada")
    try:
        owner_sid = sid_text(owner)
    finally:
        k32.LocalFree(psd)
    if owner_sid not in trusted | {SYSTEM_SID, ADMINS_SID}:
        raise ServerNotTrusted(f"La tubería del actualizador la creó otra cuenta ({owner_sid}): posible suplantación")
    # 2) Cuenta del proceso servidor, si se deja abrir (un servicio sin privilegios no puede abrir uno de SYSTEM:
    #    entonces vale la comprobación del dueño).
    pid = wintypes.ULONG()
    if not k32.GetNamedPipeServerProcessId(h, ctypes.byref(pid)):
        raise ServerNotTrusted("No se pudo saber qué proceso atiende la tubería: no se envía nada")
    hp = k32.OpenProcess(0x1000, False, pid.value)                       # PROCESS_QUERY_LIMITED_INFORMATION
    if not hp:
        return
    try:
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(hp, 0x0008, ctypes.byref(tok)):     # TOKEN_QUERY
            return
        try:
            user = token_user_sid(tok)
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(hp)
    if user not in trusted:
        raise ServerNotTrusted(f"La tubería del actualizador la atiende otra cuenta ({user}, pid {pid.value}): "
                               "posible suplantación")
