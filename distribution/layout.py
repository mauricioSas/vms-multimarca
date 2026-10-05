"""Monta el payload de una versión a partir de los artefactos de build (PLAN-V2 §2.4, §2.7 y §4.1).

Dueño: B3. Uso:

    python -m distribution.layout --version 2.0.0 --out build/layout \\
        --vmshost native/target/release/vmshost.exe --vmsctl native/target/release/vmsctl.exe \\
        --viewer native/viewer/target/release/VMS.exe --runtime-dir build/runtime --engine-dir build/engine

Salida (``--out``):

    payload/                         lo que copia el instalador a C:\\Program Files\\VMSMultimarca
      bin/vmshost.exe                arrancador fijo (CONTRATO §13.1)
      versions/<X.Y.Z>/              bin/vmsctl.exe, runtime/, app/, engine/, viewer/, models/,
                                     THIRD_PARTY_NOTICES.txt, release.json
      updater/slot-a/                vmsctl.exe + vms_updater (la ranura B la crea el actualizador)
    components/<c>-<versión>.zip     componentes del actualizador (§2.7), cada uno con MANIFEST.sha256
    components.json                  SHA-256 y tamaño de cada zip
    payload-manifest.json            SHA-256 de cada archivo del payload (el e2e lo compara con lo instalado)

Reproducibilidad (§4.1): los zips de ``app``, ``runtime``, ``models`` y ``updater`` salen idénticos en dos
builds limpias con la misma entrada: orden fijo, fecha ``SOURCE_DATE_EPOCH``, permisos fijos, sin metadatos
de usuario y ``.pyc`` compilados con ``unchecked-hash`` y una ruta de origen estable (``app/...``).
"""
from __future__ import annotations

import argparse
import compileall
import hashlib
import json
import os
import py_compile
import re
import shutil
import sys
import time
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Paquetes Python del producto que van en el componente ``app`` (CONTRATO §13.1).
APP_PACKAGES = ("vms", "analytics", "central")
#: Componentes del actualizador cuyo zip se exige reproducible (PLAN-V2 §4.1).
REPRODUCIBLE = ("app", "runtime", "models", "updater")
COMPONENTS = ("app", "runtime", "engine", "viewer", "models", "updater")
#: Servicios que reinicia cada componente al actualizarse (PLAN-V2 §2.7).
RESTART = {
    "runtime": ["VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral"],
    "app": ["VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral"],
    "engine": ["VMSEngine"],
    "viewer": [],
    "models": ["VMSAnalytics"],
    "updater": ["VMSUpdater"],
}
#: Versión de MediaMTX que se distribuye (la fija tools/fetch_mediamtx.py) y revisión de nuestro empaquetado.
ENGINE_REVISION = "r1"

VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")
_SKIP_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".pth", ".pt"}
#: Fecha mínima que admite un zip (1980-01-01). SOURCE_DATE_EPOCH anterior se sube a esta.
_ZIP_MIN_EPOCH = 315532800
#: Marca que deja el runtime de prueba: el instalador de CI con dobles no lleva Python de verdad.
RUNTIME_STUB_MARKER = "RUNTIME-DE-PRUEBA.txt"


class LayoutError(Exception):
    """Error de entrada o de montaje con un mensaje que se puede enseñar tal cual."""


def parse_version(version: str) -> tuple[int, int, int, str]:
    """``2.0.0-dev.5`` → ``(2, 0, 0, "dev.5")``. Lanza ``LayoutError`` si no es SemVer."""
    m = VERSION_RE.match(version)
    if not m:
        raise LayoutError(f"Versión no válida: {version!r} (se espera X.Y.Z o X.Y.Z-etiqueta, p. ej. 2.0.0-dev.5)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or ""


def numeric_version(version: str) -> str:
    """Versión de 4 números para el recurso de versión de Windows (``VersionInfoVersion``)."""
    major, minor, patch, _ = parse_version(version)
    return f"{major}.{minor}.{patch}.0"


def source_date_epoch(default: int | None = None) -> int:
    """``SOURCE_DATE_EPOCH`` del entorno, o ``default``, o la fecha del último commit, o 1980-01-01."""
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if raw:
        try:
            return max(int(raw), _ZIP_MIN_EPOCH)
        except ValueError as exc:
            raise LayoutError(f"SOURCE_DATE_EPOCH no es un número: {raw!r}") from exc
    if default is not None:
        return max(default, _ZIP_MIN_EPOCH)
    return max(_git_commit_epoch() or _ZIP_MIN_EPOCH, _ZIP_MIN_EPOCH)


def _git_commit_epoch() -> int | None:
    import subprocess

    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, capture_output=True, text=True,
                             timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================================== entradas
@dataclass(frozen=True)
class LayoutInputs:
    version: str
    vmshost: Path
    vmsctl: Path
    viewer: Path
    engine_dir: Path
    runtime_dir: Path | None = None
    models_dir: Path | None = None
    updater_dir: Path | None = None
    repo: Path = ROOT
    epoch: int = _ZIP_MIN_EPOCH
    compile_pyc: bool = True
    #: Metadatos opcionales que se copian a ``release.json`` (``x-build``): commit, origen de cada binario…
    build_info: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        parse_version(self.version)
        for label, path in (("vmshost.exe", self.vmshost), ("vmsctl.exe", self.vmsctl), ("VMS.exe", self.viewer)):
            if not path.is_file():
                raise LayoutError(f"Falta {label}: {path}")
        engine = self.engine_dir / "mediamtx.exe"
        if not engine.is_file():
            raise LayoutError(f"Falta el motor {engine} (python -m tools.fetch_mediamtx --platform windows_amd64 "
                              f"--dest {self.engine_dir})")
        if self.runtime_dir is not None and not self.runtime_dir.is_dir():
            raise LayoutError(f"La carpeta del runtime no existe: {self.runtime_dir}")
        for pkg in APP_PACKAGES:
            if not (self.repo / pkg / "__init__.py").is_file():
                raise LayoutError(f"No encuentro el paquete {pkg} en {self.repo}")
        if self.compile_pyc and sys.version_info[:2] != (3, 12):
            raise LayoutError("Los .pyc del producto se compilan con Python 3.12 (el del runtime). "
                              f"Este es {sys.version.split()[0]}: usa py -3.12 o --no-pyc solo para pruebas.")


@dataclass
class LayoutResult:
    out: Path
    payload: Path
    version_dir: Path
    components: dict[str, dict[str, object]]
    runtime_is_stub: bool

    @property
    def reproducible_hashes(self) -> dict[str, str]:
        return {c: str(self.components[c]["sha256"]) for c in REPRODUCIBLE if c in self.components}


# ============================================================================== copia
def _iter_files(base: Path) -> Iterator[Path]:
    """Archivos bajo ``base`` en orden estable, sin cachés ni basura del sistema."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_NAMES)
        for name in sorted(filenames):
            if name in _SKIP_NAMES or Path(name).suffix in _SKIP_SUFFIXES:
                continue
            yield Path(dirpath) / name


_JUNK = {".DS_Store", "Thumbs.db"}


def _copy_tree(src: Path, dst: Path, *, raw: bool = False) -> None:
    """Copia ``src`` en ``dst`` en orden estable.

    ``raw=False`` (código fuente del repositorio): sin cachés, ``.pyc`` ni checkpoints.
    ``raw=True`` (runtime de B1 o componentes ya preparados): todo salvo basura del sistema; un runtime lleva
    ``.pth`` y ``.pyc`` que hacen falta.
    """
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if raw or d not in _SKIP_NAMES)
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in sorted(filenames):
            if name in _JUNK or (not raw and (name in _SKIP_NAMES or Path(name).suffix in _SKIP_SUFFIXES)):
                continue
            shutil.copyfile(Path(dirpath) / name, dst / rel / name)


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _compile_app(app_dir: Path) -> None:
    """``.pyc`` en ``__pycache__`` con hash sin comprobar y ruta estable ``app/...`` (§1.2)."""
    ok = compileall.compile_dir(
        str(app_dir), ddir="app", quiet=1, workers=1, force=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    if not ok:
        raise LayoutError("Falló la compilación de los .pyc del componente app (error de sintaxis en el código)")


def _stub_runtime(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    (dst / RUNTIME_STUB_MARKER).write_text(
        "Runtime de prueba: este payload se montó sin el runtime de Python (B1, distribution/runtime).\n"
        "Solo sirve para probar el instalador con dobles; no arranca ningún servicio real.\n",
        encoding="utf-8", newline="\n")


# ============================================================================== zips reproducibles
def _manifest_lines(base: Path) -> list[str]:
    lines = []
    for f in _iter_files_all(base):
        rel = f.relative_to(base).as_posix()
        if rel == "MANIFEST.sha256":
            continue
        lines.append(f"{sha256_file(f)}  {rel}")
    return lines


def _iter_files_all(base: Path) -> Iterator[Path]:
    """Como ``_iter_files`` pero conservando ``__pycache__`` y ``.pyc`` (ya compilados por nosotros)."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def write_manifest(base: Path) -> Path:
    """``MANIFEST.sha256`` (hash por archivo, rutas con ``/``) dentro de ``base``."""
    manifest = base / "MANIFEST.sha256"
    manifest.write_text("\n".join(_manifest_lines(base)) + "\n", encoding="utf-8", newline="\n")
    return manifest


def deterministic_zip(src_dir: Path, zip_path: Path, epoch: int) -> None:
    """Zip con orden fijo, fecha fija, permisos fijos y compresión fija."""
    stamp = time.gmtime(max(epoch, _ZIP_MIN_EPOCH))[:6]
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_suffix(zip_path.suffix + ".tmp")
    files = sorted((f.relative_to(src_dir).as_posix(), f) for f in _iter_files_all(src_dir))
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, f in files:
            info = zipfile.ZipInfo(arcname, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0          # MS-DOS: sin uid/gid ni permisos POSIX
            info.external_attr = 0x20       # FILE_ATTRIBUTE_ARCHIVE
            with f.open("rb") as fh:
                zf.writestr(info, fh.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.replace(tmp, zip_path)


def _set_mtimes(base: Path, epoch: int) -> None:
    for f in _iter_files_all(base):
        os.utime(f, (epoch, epoch))


# ============================================================================== montaje
def _engine_version() -> str:
    try:
        from tools.fetch_mediamtx import VERSION
    except ImportError:  # pragma: no cover - el repositorio siempre lo trae
        VERSION = "v0.0.0"
    return f"{VERSION.lstrip('v')}-{ENGINE_REVISION}"


def _runtime_version(runtime: Path, stub: bool) -> str:
    if stub:
        return "stub"
    for name in ("VERSION", "RUNTIME-VERSION.txt"):
        f = runtime / name
        if f.is_file():
            value = f.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if value:
                return value
    # Sin archivo de versión (B1 lo añadirá): versión por contenido, estable entre builds idénticas.
    return "c" + _tree_digest(runtime)[:12]


def _tree_digest(base: Path) -> str:
    h = hashlib.sha256()
    for line in _manifest_lines(base):
        h.update(line.encode("utf-8") + b"\n")
    return h.hexdigest()


def build_layout(inputs: LayoutInputs, out: Path, *, clean: bool = True) -> LayoutResult:
    """Monta ``payload/`` y los zips de componentes. Devuelve los hashes para comprobar la reproducibilidad."""
    inputs.validate()
    if clean and out.exists():
        shutil.rmtree(out)
    payload = out / "payload"
    vdir = payload / "versions" / inputs.version
    staging = out / "staging"

    # --- carpeta fija: el arrancador
    _copy_file(inputs.vmshost, payload / "bin" / "vmshost.exe")

    # --- componentes en staging (cada uno es lo que va dentro de su zip)
    app = staging / "app"
    for pkg in APP_PACKAGES:
        _copy_tree(inputs.repo / pkg, app / pkg)
    if inputs.compile_pyc:
        _compile_app(app)

    runtime = staging / "runtime"
    stub = inputs.runtime_dir is None
    if stub:
        _stub_runtime(runtime)
    else:
        assert inputs.runtime_dir is not None
        _copy_tree(inputs.runtime_dir, runtime, raw=True)

    engine = staging / "engine"
    _copy_file(inputs.engine_dir / "mediamtx.exe", engine / "mediamtx.exe")
    for lic in ("MEDIAMTX-LICENSE.txt", "LICENSE"):
        if (inputs.engine_dir / lic).is_file():
            _copy_file(inputs.engine_dir / lic, engine / "MEDIAMTX-LICENSE.txt")
            break
    else:
        raise LayoutError(f"Falta la licencia de MediaMTX (LICENSE) en {inputs.engine_dir}: es obligatoria al "
                          "redistribuirlo")

    viewer = staging / "viewer"
    _copy_file(inputs.viewer, viewer / "VMS.exe")

    models = staging / "models"
    models.mkdir(parents=True, exist_ok=True)
    models_src = inputs.models_dir if inputs.models_dir is not None else inputs.repo / "models"
    if models_src.is_dir():
        for f in _iter_files(models_src):
            rel = f.relative_to(models_src)
            if rel.parts and rel.parts[0] == "weights":
                continue   # checkpoints de exportación: nunca se distribuyen
            _copy_file(f, models / rel)

    updater = staging / "updater"
    _copy_file(inputs.vmsctl, updater / "vmsctl.exe")
    updater_src = inputs.updater_dir if inputs.updater_dir is not None else inputs.repo / "updater" / "vms_updater"
    if updater_src.is_dir():
        _copy_tree(updater_src, updater / "vms_updater")

    # --- versiones de componente y zips
    versions = {
        "app": inputs.version,
        "runtime": _runtime_version(runtime, stub),
        "engine": _engine_version(),
        "viewer": inputs.version,
        "models": "m" + _tree_digest(models)[:12],
        "updater": inputs.version,
    }
    sources = {"app": app, "runtime": runtime, "engine": engine, "viewer": viewer, "models": models,
               "updater": updater}
    components: dict[str, dict[str, object]] = {}
    for name in COMPONENTS:
        src = sources[name]
        write_manifest(src)
        _set_mtimes(src, inputs.epoch)
        zpath = out / "components" / f"{name}-{versions[name]}.zip"
        deterministic_zip(src, zpath, inputs.epoch)
        components[name] = {
            "version": versions[name],
            "target": f"components/{name}/{zpath.name}",
            "file": zpath.relative_to(out).as_posix(),
            "sha256": sha256_file(zpath),
            "length": zpath.stat().st_size,
            "restart": RESTART[name],
        }
        if name == "engine":
            components[name]["recording_gap"] = True

    # --- payload/versions/<X>: lo mismo que los zips, ya descomprimido
    _copy_file(inputs.vmsctl, vdir / "bin" / "vmsctl.exe")
    for name, sub in (("runtime", "runtime"), ("app", "app"), ("engine", "engine"), ("viewer", "viewer"),
                      ("models", "models")):
        _copy_tree(sources[name], vdir / sub, raw=True)
    notices = inputs.repo / "THIRD_PARTY_NOTICES.txt"
    if notices.is_file():
        _copy_file(notices, vdir / "THIRD_PARTY_NOTICES.txt")
    release = release_descriptor(inputs, components)
    (vdir / "release.json").write_text(json.dumps(release, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8", newline="\n")
    _copy_tree(sources["updater"], payload / "updater" / "slot-a", raw=True)

    _set_mtimes(payload, inputs.epoch)
    (out / "components.json").write_text(
        json.dumps({"version": inputs.version, "epoch": inputs.epoch, "components": components},
                   indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    write_payload_manifest(payload, out / "payload-manifest.json", inputs.version)
    shutil.rmtree(staging)
    return LayoutResult(out=out, payload=payload, version_dir=vdir, components=components, runtime_is_stub=stub)


def release_descriptor(inputs: LayoutInputs, components: dict[str, dict[str, object]]) -> dict[str, object]:
    """Descriptor de versión (PLAN-V2 §2.7, esquema 1) **sin firmar**: B4 lo firma al publicar.

    Lo que solo sabe quien publica (notas, ``min_from``, severidad, emisores de Authenticode) va vacío o con el
    valor prudente; ``x-build`` deja constancia de que es la copia de build, no la publicada.
    """
    return {
        "schema": 1,
        "product": "vms-multimarca",
        "version": inputs.version,
        "min_from": "2.0.0",
        "security": False,
        "severity": "none",
        "notes_es": "",
        "config_schema": 2,
        "requires": {"windows_build_min": 19045, "webview2_min": "130.0.0.0"},
        "components": {name: {k: v for k, v in comp.items() if k != "file"} for name, comp in components.items()},
        "x-build": {"unsigned": True, "epoch": inputs.epoch, **inputs.build_info},
    }


def payload_files(payload: Path) -> dict[str, dict[str, object]]:
    return {f.relative_to(payload).as_posix(): {"sha256": sha256_file(f), "size": f.stat().st_size}
            for f in _iter_files_all(payload)}


def write_payload_manifest(payload: Path, dest: Path, version: str) -> None:
    dest.write_text(json.dumps({"version": version, "files": payload_files(payload)}, indent=1, sort_keys=True)
                    + "\n", encoding="utf-8", newline="\n")


def compare_builds(a: Path, b: Path, names: Iterable[str] = REPRODUCIBLE) -> dict[str, tuple[str, str]]:
    """Componentes cuyo zip difiere entre dos salidas de ``build_layout`` → ``{nombre: (hash_a, hash_b)}``."""
    ca = json.loads((a / "components.json").read_text(encoding="utf-8"))["components"]
    cb = json.loads((b / "components.json").read_text(encoding="utf-8"))["components"]
    diff = {}
    for name in names:
        ha, hb = str(ca.get(name, {}).get("sha256", "")), str(cb.get(name, {}).get("sha256", ""))
        if not ha or ha != hb:
            diff[name] = (ha, hb)
    return diff


# ============================================================================== CLI
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m distribution.layout", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--vmshost", type=Path, required=True)
    p.add_argument("--vmsctl", type=Path, required=True)
    p.add_argument("--viewer", type=Path, required=True)
    p.add_argument("--engine-dir", type=Path, required=True, help="carpeta con mediamtx.exe y LICENSE")
    p.add_argument("--runtime-dir", type=Path, help="runtime de B1; sin él se monta un runtime de prueba")
    p.add_argument("--models-dir", type=Path)
    p.add_argument("--updater-dir", type=Path, help="paquete vms_updater (por defecto updater/vms_updater)")
    p.add_argument("--epoch", type=int, help="SOURCE_DATE_EPOCH (por defecto, la fecha del último commit)")
    p.add_argument("--no-pyc", action="store_true", help="no compilar .pyc (solo para pruebas)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = LayoutInputs(version=args.version, vmshost=args.vmshost, vmsctl=args.vmsctl, viewer=args.viewer,
                              engine_dir=args.engine_dir, runtime_dir=args.runtime_dir, models_dir=args.models_dir,
                              updater_dir=args.updater_dir, epoch=source_date_epoch(args.epoch),
                              compile_pyc=not args.no_pyc)
        result = build_layout(inputs, args.out)
    except LayoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for name, comp in result.components.items():
        print(f"{name:8} {comp['version']:>20}  {comp['sha256']}  {comp['length']} B")
    if result.runtime_is_stub:
        print("AVISO: runtime de prueba (sin Python). Este payload solo sirve para probar el instalador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
