"""Firma Authenticode del payload, del instalador y del desinstalador (PLAN-V2 §1.6 y decisión N1 de §9).

Mientras no haya certificado (decisión D1), **no se firma nada** y la orden lo dice sin fallar: la build sale
sin firmar y SmartScreen avisará en el laboratorio. El hueco queda preparado para dos herramientas:

- **jsign** (Apache-2.0; Linux, macOS o Windows), la de producción con Certum o con la llave física:
  ``VMS_JSIGN_JAR``, ``VMS_SIGN_STORETYPE`` (p. ej. ``CERTUM``, ``PIV``, ``PKCS12``), ``VMS_SIGN_KEYSTORE``,
  ``VMS_SIGN_ALIAS``, ``VMS_SIGN_STOREPASS`` (se pasa a jsign como ``env:``, nunca por la línea de órdenes) y
  ``VMS_SIGN_TSA`` (sello de tiempo RFC 3161, obligatorio: §1.6).
- **signtool** con un ``.pfx`` de prueba (solo CI, certificado autofirmado importado en el runner):
  ``VMS_SIGN_PFX`` y ``VMS_SIGN_PFX_PASSWORD``.

Se firma todo PE nuestro o redistribuido **sin firma** (vmshost, vmsctl, VMS.exe, mediamtx.exe y los ``.pyd``
o ``.dll`` de wheels que no traigan firma). Lo que ya viene firmado (``python.exe`` de la PSF) no se toca.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

PE_SUFFIXES = {".exe", ".dll", ".pyd"}
DEFAULT_TSA = "http://time.certum.pl"


@dataclass(frozen=True)
class SignConfig:
    tool: str                      # "jsign" | "signtool"
    command_prefix: tuple[str, ...]
    env: Mapping[str, str]


def has_authenticode_signature(path: Path) -> bool:
    """¿Tiene el PE una tabla de certificados (directorio de seguridad) no vacía?

    Basta para decidir si hay que firmarlo; la validez la comprueba ``WinVerifyTrust`` en el e2e.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
            if len(head) < 64 or head[:2] != b"MZ":
                return False
            pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
            fh.seek(pe_offset)
            if fh.read(4) != b"PE\0\0":
                return False
            fh.seek(pe_offset + 24)
            magic = struct.unpack("<H", fh.read(2))[0]
            # Directorio de datos 4 (certificados): offset 128 (PE32) o 144 (PE32+) desde la cabecera opcional.
            dd_offset = pe_offset + 24 + (128 if magic == 0x10B else 144)
            fh.seek(dd_offset)
            data = fh.read(8)
            if len(data) < 8:
                return False
            size = int(struct.unpack("<II", data)[1])
            return size > 0
    except OSError:
        return False


def unsigned_pe_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in PE_SUFFIXES
                  and not has_authenticode_signature(p))


def _find_signtool() -> str | None:
    explicit = os.environ.get("VMS_SIGNTOOL")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("signtool")
    if found:
        return found
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    if kits.is_dir():
        for cand in sorted(kits.glob("10.*/x64/signtool.exe"), reverse=True):
            return str(cand)
    return None


def config_from_env(env: Mapping[str, str] | None = None) -> SignConfig | None:
    """Configuración de firma o ``None`` si no hay certificado (caso normal hasta D1)."""
    env = os.environ if env is None else env
    jar = env.get("VMS_JSIGN_JAR", "")
    if jar:
        missing = [k for k in ("VMS_SIGN_STORETYPE", "VMS_SIGN_KEYSTORE", "VMS_SIGN_ALIAS") if not env.get(k)]
        if missing:
            raise ValueError("Para firmar con jsign faltan: " + ", ".join(missing))
        java = env.get("VMS_JAVA") or shutil.which("java") or "java"
        prefix = [java, "-jar", jar, "--storetype", env["VMS_SIGN_STORETYPE"], "--keystore",
                  env["VMS_SIGN_KEYSTORE"], "--alias", env["VMS_SIGN_ALIAS"],
                  "--tsaurl", env.get("VMS_SIGN_TSA") or DEFAULT_TSA, "--tsmode", "RFC3161",
                  "--alg", "SHA-256"]
        if env.get("VMS_SIGN_STOREPASS"):
            prefix += ["--storepass", "env:VMS_SIGN_STOREPASS"]
        return SignConfig("jsign", tuple(prefix), {})
    pfx = env.get("VMS_SIGN_PFX", "")
    if pfx:
        tool = _find_signtool()
        if tool is None:
            raise ValueError("VMS_SIGN_PFX está definido pero no encuentro signtool.exe (Windows SDK)")
        prefix = [tool, "sign", "/fd", "SHA256", "/f", pfx]
        if env.get("VMS_SIGN_PFX_PASSWORD"):
            prefix += ["/p", env["VMS_SIGN_PFX_PASSWORD"]]
        if env.get("VMS_SIGN_TSA"):
            prefix += ["/tr", env["VMS_SIGN_TSA"], "/td", "SHA256"]
        return SignConfig("signtool", tuple(prefix), {})
    return None


def sign_files(files: Iterable[Path], config: SignConfig) -> None:
    for f in files:
        proc = subprocess.run([*config.command_prefix, str(f)], capture_output=True, text=True, timeout=300,
                              check=False)
        if proc.returncode != 0:
            # La salida de jsign/signtool no lleva la contraseña (va por env: o ya está en el prefijo, que no
            # se imprime).
            raise RuntimeError(f"La firma de {f.name} falló (código {proc.returncode}): "
                               f"{(proc.stdout + proc.stderr).strip()[-1500:]}")


def sign_tree(base: Path, *, env: Mapping[str, str] | None = None, dry_run: bool = False) -> tuple[str, list[Path]]:
    """Firma los PE sin firma de ``base``. Devuelve (estado, archivos) con estado ``skipped``/``planned``/``signed``."""
    files = unsigned_pe_files(base)
    config = config_from_env(env)
    if config is None:
        return "skipped", files
    if dry_run:
        return "planned", files
    sign_files(files, config)
    return "signed", files


def sign_one_cli(path: str) -> int:
    """Lo usa ISCC (``SignTool=``) para el instalador y el desinstalador: ``$f`` → este archivo."""
    config = config_from_env()
    if config is None:
        print(f"Sin certificado: {Path(path).name} queda sin firmar (decisión N1).", file=sys.stderr)
        return 0
    sign_files([Path(path)], config)
    return 0
