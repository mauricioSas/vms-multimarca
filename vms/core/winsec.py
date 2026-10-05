"""Secretos con DPAPI de máquina en Windows (CONTRATO §13.9; gemelo de `vms_common::secret` en Rust).

Archivos de `secrets\\` (`secret.key`, `credentials.enc`, `internal.token`, `kiosk.token`, `site.token`,
`evidence-ed25519.key`):

- Formato en disco: una línea ASCII `vms-dpapi-v1:<base64 del blob de CryptProtectData>`. Cualquier otro
  contenido es un secreto **en claro** de la v1: se sigue leyendo y se migra al formato protegido.
- El blob se crea con `CRYPTPROTECT_LOCAL_MACHINE` y una entropía fija del producto. Cualquier proceso del
  equipo puede descifrarlo, así que **la barrera real es la ACL** del archivo (solo SYSTEM, Administradores
  y el SID del servicio que lo usa; la aplica `vmsctl acl apply`). DPAPI añade que una copia del archivo
  fuera del equipo (un respaldo, un disco robado) no sirve.
- No se usa DPAPI-NG con `SID=`: es para grupos de un dominio de AD y los PC de tienda no están en dominio.
- Fuera de Windows (desarrollo) no hay DPAPI: se escribe en claro con permisos 0600, como en la v1.
- En SSD el borrado seguro del archivo en claro **no** está garantizado (se sobrescribe antes de
  sustituirlo, pero la controladora puede conservar copias): se documenta.

Sin dependencias: `ctypes` sobre `crypt32.dll`.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import sys
from pathlib import Path

from .atomic import atomic_write_bytes
from .paths import restrict_permissions

log = logging.getLogger("vms.core.winsec")

PREFIX = b"vms-dpapi-v1:"
ENTROPY = b"VMSMultimarca/secret/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


class SecretProtectionError(RuntimeError):
    """No se pudo proteger o recuperar un secreto con DPAPI."""


def dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":  # pragma: no cover - solo Windows (lo prueba el job de Windows de B1)
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _dpapi_win(data: bytes, protect: bool) -> bytes:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        in_buf = ctypes.create_string_buffer(data, len(data))
        ent_buf = ctypes.create_string_buffer(ENTROPY, len(ENTROPY))
        data_in = _DataBlob(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
        entropy = _DataBlob(len(ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char)))
        out = _DataBlob()
        if protect:
            fn = crypt32.CryptProtectData
            flags = _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE
        else:
            fn = crypt32.CryptUnprotectData
            flags = _CRYPTPROTECT_UI_FORBIDDEN
        fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(data_in), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(out)):
            err = ctypes.get_last_error()
            what = "proteger" if protect else "recuperar"
            raise SecretProtectionError(f"DPAPI no pudo {what} el secreto (código de Windows {err})")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData con ámbito de máquina."""
    if sys.platform == "win32":  # pragma: no cover - solo Windows
        return _dpapi_win(data, protect)
    raise SecretProtectionError("DPAPI solo existe en Windows")


def protect(data: bytes) -> bytes:
    return _dpapi(data, protect=True)


def unprotect(blob: bytes) -> bytes:
    return _dpapi(blob, protect=False)


def encode_protected(blob: bytes) -> bytes:
    return PREFIX + base64.b64encode(blob) + b"\n"


def parse(raw: bytes) -> tuple[bool, bytes]:
    """(protegido, contenido): el blob DPAPI si lleva el prefijo, o el secreto en claro tal cual."""
    stripped = raw.strip()
    if stripped.startswith(PREFIX):
        try:
            return True, base64.b64decode(stripped[len(PREFIX):], validate=True)
        except ValueError:
            log.warning("Un archivo de secreto empieza como protegido pero no es base64 válido: se trata como texto")
    return False, raw


def is_protected(path: Path) -> bool:
    try:
        return parse(Path(path).read_bytes())[0]
    except OSError:
        return False


def read_secret(path: Path) -> bytes:
    """Secreto en claro, venga protegido o no. FileNotFoundError si no existe."""
    protected, payload = parse(Path(path).read_bytes())
    return unprotect(payload) if protected else payload


def read_secret_text(path: Path) -> str:
    return read_secret(path).decode("utf-8").strip()


def dump(data: bytes) -> bytes:
    """Bytes que van al disco: protegidos con DPAPI de máquina en Windows; tal cual en el resto."""
    return encode_protected(protect(data)) if dpapi_available() else data


def write_secret(path: Path, data: bytes) -> None:
    """Protegido con DPAPI de máquina en Windows; en claro (0600) en el resto."""
    path = Path(path)
    atomic_write_bytes(path, dump(data))
    restrict_permissions(path)


def _overwrite_in_place(path: Path) -> None:
    """Sobrescribe el contenido en claro antes de sustituir el archivo (en SSD no hay garantía)."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(os.urandom(size))
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        log.warning("No se pudo sobrescribir %s antes de protegerlo: %s", path.name, exc)


def migrate_plaintext(path: Path) -> bool:
    """Si `path` es un secreto en claro de la v1 y hay DPAPI, lo protege. True si lo migró."""
    path = Path(path)
    if not dpapi_available() or not path.is_file():
        return False
    raw = path.read_bytes()
    protected, payload = parse(raw)
    if protected:
        return False
    blob = encode_protected(protect(payload))
    _overwrite_in_place(path)
    atomic_write_bytes(path, blob)
    log.info("Secreto %s protegido con DPAPI de máquina (migración desde la v1)", path.name)
    return True


def service_sid(name: str) -> str:
    """SID de `NT SERVICE\\<name>` (S-1-5-80-…), igual que `vms_common::sid::service_sid`."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le"), usedforsecurity=False).digest()
    parts = [str(int.from_bytes(digest[i:i + 4], "little")) for i in range(0, 20, 4)]
    return "S-1-5-80-" + "-".join(parts)
