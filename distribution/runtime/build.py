"""Runtime de Windows: Python 3.12 embebible oficial + `site-packages` preparado aquí (PLAN-V2 §1.2).

    python -m distribution.runtime.build --out build/runtime
    python -m distribution.runtime.build --out build/runtime --requirements requirements-vms.txt   (mínimo)

Pasos:
1. `python-3.12.10-embed-amd64.zip` de python.org con su SHA-256 fijado (o `--python-zip`, sin red).
2. `python312._pth` con `python312.zip`, `.`, `Lib\\site-packages`, `..\\app` e `import site`: el código del
   producto vive en `versions\\<X>\\app` (§2.4) y el intérprete no mira PYTHONPATH ni el registro.
3. `pip install --target Lib\\site-packages --platform win_amd64 --python-version 3.12 --implementation cp
   --abi cp312 --only-binary=:all: --no-deps --require-hashes` con los archivos de bloqueo. Los marcadores
   de entorno (`; sys_platform == "win32"`) se evalúan **para Windows** aunque se construya en macOS o
   Linux (pip los evaluaría para el equipo que construye).
4. `.pyc` compilados aquí con `--invalidation-mode unchecked-hash` y rutas relativas: los servicios corren
   con `PYTHONDONTWRITEBYTECODE=1` y el intérprete nunca escribe dentro de `versions\\` (§2.4).
5. `MANIFEST.sha256` (un hash por archivo, rutas con `/`, ordenadas): el actualizador lo comprueba tras
   descomprimir y dos construcciones con las mismas entradas dan el mismo manifiesto.

En el PC del cliente nunca se ejecuta pip: instalar es copiar archivos verificados.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_VERSION = "3.12.10"
PYTHON_ZIP = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ZIP}"
# Mismo archivo que ya verificó la v1 (deploy/windows/install.ps1) en la primera instalación real.
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
PTH_NAME = "python312._pth"
PTH_LINES = ["python312.zip", ".", r"Lib\site-packages", r"..\app", "import site"]
DEFAULT_REQUIREMENTS = ["requirements-vms.txt", "requirements-analytics.txt", "requirements-central.txt",
                        "distribution/runtime/requirements-windows-extra.txt"]
MANIFEST = "MANIFEST.sha256"
# Entorno de destino para evaluar marcadores (el mismo que tools.lock_requirements usa para «win32»).
WIN_ENV = {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt", "platform_machine": "AMD64",
           "implementation_name": "cpython", "platform_python_implementation": "CPython", "python_version": "3.12",
           "python_full_version": PYTHON_VERSION}
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)")


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _marker_ok(marker: str) -> bool:
    try:
        from packaging.markers import Marker
    except ImportError:  # pragma: no cover - en un Python sin «packaging» se usa el que trae pip
        from pip._vendor.packaging.markers import Marker  # type: ignore[no-redef]
    return bool(Marker(marker).evaluate(WIN_ENV))


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    line: str          # «nombre==versión --hash=… --hash=…» sin marcador


def windows_pins(files: list[Path]) -> list[Pin]:
    """Requisitos de los archivos de bloqueo que aplican a Windows, sin marcadores y sin duplicados."""
    pins: dict[str, Pin] = {}
    for file in files:
        text = file.read_text(encoding="utf-8")
        logical = re.sub(r"\\\s*\n", " ", text)
        for raw in logical.splitlines():
            line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
            if not line:
                continue
            if line.startswith("-"):
                raise BuildError(f"{file.name}: opción no admitida en un lock: «{line[:40]}»")
            spec, _, hashes = line.partition("--hash")
            hashes = ("--hash" + hashes).strip() if hashes else ""
            spec, _, marker = spec.partition(";")
            if marker.strip() and not _marker_ok(marker.strip()):
                continue
            m = _REQ_NAME.match(spec)
            if not m:
                raise BuildError(f"{file.name}: requisito sin versión fijada: «{spec.strip()}»")
            if not hashes:
                raise BuildError(f"{file.name}: «{m.group(1)}» sin --hash (los locks de sede llevan SHA-256)")
            name = m.group(1).lower().replace("_", "-")
            pin = Pin(name, m.group(2), f"{m.group(1)}=={m.group(2)} {hashes}")
            old = pins.get(name)
            if old is not None and old.version != pin.version:
                raise BuildError(f"versiones distintas de {name} en los locks: {old.version} y {pin.version}")
            pins[name] = pin
    return [pins[k] for k in sorted(pins)]


def _download(url: str, dest: Path) -> None:
    """httpx (con los certificados de certifi) si está; si no, urllib (almacén del sistema)."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - el runtime de CI de Windows trae los certificados del sistema
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:  # noqa: S310
            shutil.copyfileobj(r, f)
        return
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r, open(dest, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    except httpx.HTTPError as exc:
        raise OSError(str(exc)) from exc


def fetch_python(cache: Path, expected_sha: str, local_zip: Path | None) -> Path:
    if local_zip is not None:
        zip_path = local_zip
    else:
        cache.mkdir(parents=True, exist_ok=True)
        zip_path = cache / PYTHON_ZIP
        if not zip_path.is_file() or sha256_file(zip_path) != expected_sha:
            print(f"Descargando {PYTHON_URL}")
            tmp = zip_path.with_suffix(".part")
            try:
                _download(PYTHON_URL, tmp)
            except OSError as exc:
                raise BuildError(f"no se pudo descargar {PYTHON_URL}: {exc}. Sin red, usa --python-zip") from exc
            os.replace(tmp, zip_path)
    got = sha256_file(zip_path)
    if got != expected_sha:
        raise BuildError(f"SHA-256 incorrecto para {zip_path.name}: esperado {expected_sha}, obtenido {got}")
    return zip_path


def pip_install(site: Path, requirements: Path, *, find_links: list[str], no_index: bool) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "--target", str(site), "--platform", "win_amd64",
           "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312", "--only-binary=:all:",
           "--no-deps", "--require-hashes", "--no-compile", "--disable-pip-version-check",
           "--no-warn-script-location", "--upgrade", "-r", str(requirements)]
    for fl in find_links:
        cmd += ["--find-links", fl]
    if no_index:
        cmd.append("--no-index")
    env = dict(os.environ, PIP_NO_INPUT="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PIP_REQUIRE_VIRTUALENV", None)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
        raise BuildError(f"pip install falló (código {r.returncode}):\n{tail}")


def clean_site(site: Path) -> None:
    """Fuera lo que depende del equipo que construye: lanzadores de órdenes y cachés."""
    for name in ("bin", "Scripts"):
        shutil.rmtree(site / name, ignore_errors=True)
    for cache in site.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def compile_site(site: Path) -> None:
    """`.pyc` con `unchecked-hash` y rutas relativas (`Lib/site-packages/...`), igual en cualquier equipo.

    En un proceso aparte con `PYTHONHASHSEED=0`: el orden de los conjuntos constantes no varía entre
    construcciones."""
    env = dict(os.environ, PYTHONHASHSEED="0")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    cmd = [sys.executable, "-m", "compileall", "-q", "-f", "-j", "0", "-d", "Lib/site-packages",
           "--invalidation-mode", "unchecked-hash", str(site)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print("Aviso: algunos archivos .py no compilan (p. ej. plantillas o código para otras versiones); "
              "se dejan sin .pyc")


def write_manifest(root: Path) -> Path:
    lines = []
    for p in sorted(root.rglob("*"), key=lambda q: q.relative_to(root).as_posix()):
        if p.is_file() and p.name != MANIFEST:
            lines.append(f"{sha256_file(p)}  {p.relative_to(root).as_posix()}")
    out = root / MANIFEST
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return out


def verify_manifest(root: Path) -> list[str]:
    """Problemas encontrados (lista vacía = todo bien). Lo mismo que hará el actualizador."""
    problems = []
    listed = set()
    for line in (root / MANIFEST).read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        listed.add(rel)
        f = root / rel
        if not f.is_file():
            problems.append(f"falta {rel}")
        elif sha256_file(f) != digest:
            problems.append(f"modificado {rel}")
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and p.name != MANIFEST and rel not in listed:
            problems.append(f"sobra {rel}")
    return problems


def build(out: Path, requirements: list[Path], *, cache: Path, python_zip: Path | None = None,
          python_sha256: str = PYTHON_SHA256, find_links: list[str] | None = None,
          no_index: bool = False) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise BuildError("Hay que construir con Python 3.12: los .pyc dependen de la versión del intérprete")
    zip_path = fetch_python(cache, python_sha256, python_zip)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    (out / PTH_NAME).write_text("\r\n".join(PTH_LINES) + "\r\n", encoding="ascii", newline="")
    site = out / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    pins = windows_pins(requirements)
    with tempfile.TemporaryDirectory() as td:
        req = Path(td) / "requirements-windows.txt"
        req.write_text("\n".join(p.line for p in pins) + "\n", encoding="utf-8")
        if pins:
            pip_install(site, req, find_links=find_links or [], no_index=no_index)
    clean_site(site)
    compile_site(site)
    write_manifest(out)
    print(f"Runtime listo en {out}: Python {PYTHON_VERSION} embebible + {len(pins)} paquetes")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m distribution.runtime.build", description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="carpeta del runtime (se vacía)")
    ap.add_argument("--requirements", action="append", type=Path, default=None,
                    help="archivo de bloqueo (se puede repetir; por defecto vms + analytics + central + extra)")
    ap.add_argument("--cache", type=Path, default=ROOT / "build" / "cache", help="descargas")
    ap.add_argument("--python-zip", type=Path, default=None, help="Python embebible ya descargado (sin red)")
    ap.add_argument("--python-sha256", default=PYTHON_SHA256, help=argparse.SUPPRESS)
    ap.add_argument("--find-links", action="append", default=[], help="carpeta con wheels (sin red)")
    ap.add_argument("--no-index", action="store_true", help="no usar PyPI (solo --find-links)")
    ap.add_argument("--verify", action="store_true", help="solo comprobar MANIFEST.sha256 de --out")
    a = ap.parse_args(argv)
    if a.verify:
        problems = verify_manifest(a.out)
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    reqs = a.requirements or [ROOT / r for r in DEFAULT_REQUIREMENTS]
    try:
        build(a.out, reqs, cache=a.cache, python_zip=a.python_zip, python_sha256=a.python_sha256,
              find_links=a.find_links, no_index=a.no_index)
    except BuildError as exc:
        print(f"No se pudo construir el runtime: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

