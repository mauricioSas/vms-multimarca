"""Inno Setup: instalación verificada del compilador y llamada a ISCC (PLAN-V2 §1.4)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from . import INNO_SHA256, INNO_URL, INNO_VERSION, ISS_FILE, ROOT


class BuildError(Exception):
    """Fallo de build con un mensaje que se puede enseñar tal cual."""


def default_inno_dir() -> Path:
    base = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return Path(base) / "Inno Setup 7"


def find_iscc(explicit: str | None = None) -> Path | None:
    """``VMS_ISCC`` o ``--iscc``, luego las rutas de instalación habituales, luego el PATH."""
    candidates = [explicit, os.environ.get("VMS_ISCC")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates += [str(Path(base) / "Inno Setup 7" / "ISCC.exe")]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    found = shutil.which("ISCC") or shutil.which("iscc")
    return Path(found) if found else None


def iscc_version(iscc: Path) -> str:
    """Versión que imprime ``ISCC --version`` (opción nueva de Inno 7)."""
    out = subprocess.run([str(iscc), "--version"], capture_output=True, text=True, timeout=60, check=False)
    text = (out.stdout + out.stderr).strip()
    for token in text.replace(",", " ").split():
        if token[:1].isdigit() and token.count(".") >= 2:
            return token.strip()
    return text


def download_verified(url: str, sha256: str, dest: Path) -> Path:
    """Descarga ``url`` a ``dest`` solo si su SHA-256 coincide (si ya está y coincide, no descarga)."""
    if dest.is_file() and _sha256(dest) == sha256:
        return dest
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    got = _sha256(tmp)
    if got != sha256:
        tmp.unlink(missing_ok=True)
        raise BuildError(f"SHA-256 incorrecto para {url}: esperado {sha256}, obtenido {got}. Descarga descartada.")
    os.replace(tmp, dest)
    return dest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_inno(target: Path | None = None, cache: Path | None = None) -> Path:
    """Instala Inno Setup 7.1.0 (descarga verificada) en ``target`` y devuelve la ruta de ISCC.exe."""
    if sys.platform != "win32":
        raise BuildError("Inno Setup solo se instala y se ejecuta en Windows (PLAN-V2 §1.8: desde el Mac no se "
                         "compila el instalador).")
    target = target or default_inno_dir()
    iscc = target / "ISCC.exe"
    if iscc.is_file() and iscc_version(iscc).startswith(INNO_VERSION):
        return iscc
    cache = cache or ROOT / ".tmp" / "downloads"
    setup = download_verified(INNO_URL, INNO_SHA256, cache / Path(INNO_URL).name)
    cmd = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={target}",
           "/LOG=" + str(cache / "innosetup-install.log")]
    proc = subprocess.run(cmd, timeout=600, check=False)
    if proc.returncode != 0 or not iscc.is_file():
        raise BuildError(f"La instalación de Inno Setup {INNO_VERSION} falló (código {proc.returncode}); "
                         f"registro en {cache / 'innosetup-install.log'}")
    version = iscc_version(iscc)
    if not version.startswith(INNO_VERSION):
        raise BuildError(f"ISCC instalado informa la versión {version!r}, se esperaba {INNO_VERSION}")
    return iscc


def iscc_defines(*, version: str, numeric: str, payload: Path, output_dir: Path, base_name: str,
                 test_build: bool, sign_tool: str | None = None) -> dict[str, str]:
    defines = {
        "AppVersion": version,
        "AppVersionNumeric": numeric,
        "PayloadDir": str(payload.resolve()),
        "OutputDir": str(output_dir.resolve()),
        "OutputBaseName": base_name,
    }
    if test_build:
        defines["TestBuild"] = "1"
    if sign_tool:
        defines["SignToolName"] = sign_tool
    return defines


def iscc_command(iscc: Path, defines: Mapping[str, str], *, sign_tools: Mapping[str, str] | None = None,
                 iss: Path = ISS_FILE) -> list[str]:
    """Orden de ISCC. ``/Qp``: silencioso salvo progreso y errores (PLAN-V2 §4.5)."""
    cmd = [str(iscc), "/Qp"]
    cmd += [f"/D{k}={v}" for k, v in defines.items()]
    for name, command in (sign_tools or {}).items():
        cmd.append(f"/S{name}={command}")
    cmd.append(str(iss))
    return cmd


def compile_installer(iscc: Path, defines: Mapping[str, str], *, sign_tools: Mapping[str, str] | None = None,
                      log: Path | None = None) -> Path:
    cmd = iscc_command(iscc, defines, sign_tools=sign_tools)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    output = proc.stdout + proc.stderr
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(output, encoding="utf-8")
    if proc.returncode != 0:
        raise BuildError(f"ISCC falló (código {proc.returncode}):\n{output[-4000:]}")
    exe = Path(defines["OutputDir"]) / f"{defines['OutputBaseName']}.exe"
    if not exe.is_file():
        raise BuildError(f"ISCC terminó bien pero no encuentro {exe}")
    return exe
