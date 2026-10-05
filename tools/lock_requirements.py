"""Genera requirements-<extra>.txt fijados a partir del entorno virtual actual.

    python -m tools.lock_requirements

Para cada extra de pyproject.toml ([vms], [analytics], [central], [dev]) calcula el cierre
de dependencias recorriendo los metadatos de los paquetes instalados y fija la versión
instalada. Las dependencias que solo aplican en otra plataforma (p. ej. pywin32-ctypes en
Windows) se escriben con su marcador de entorno; si no están instaladas aquí se fijan con la
restricción declarada y una nota: conviene regenerar el lock en la plataforma de destino.

Paquetes EXCLUIDOS a propósito (ver docs/CONTRATO.md §10, licencias):
  - av (PyAV): sus wheels traen FFmpeg GPL. supervision lo declara, pero solo lo usa en
    utilidades de vídeo que no empleamos.
  - opencv-python: sustituido por opencv-python-headless (mismo módulo cv2, sin GUI).
Por eso los lock se instalan con:  pip install --no-deps -r requirements-<extra>.txt

Integridad: los lock que se instalan en las sedes ([vms], [analytics], [central]) llevan los
SHA-256 de TODOS los archivos publicados en PyPI de cada versión (wheels de Windows, Linux y
macOS, y el código fuente). Con hashes, pip entra solo en modo «--require-hashes» y rechaza
cualquier archivo que no coincida. Las dependencias de otra plataforma que no están instaladas
aquí se fijan a la versión más reciente de PyPI que cumple lo declarado. Sin red:
    python -m tools.lock_requirements --no-hashes
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"av", "opencv-python"}
SELF = "vms-multimarca"
HASHED = {"vms", "analytics", "central", "updater"}   # los que instalan los instaladores de sede (+ ranuras del actualizador)
PYPI = "https://pypi.org/pypi"

TARGETS = {
    "win32": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
              "platform_machine": "AMD64", "implementation_name": "cpython",
              "platform_python_implementation": "CPython", "python_version": "3.12",
              "python_full_version": "3.12.3"},
    "linux": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
              "platform_machine": "x86_64", "implementation_name": "cpython",
              "platform_python_implementation": "CPython", "python_version": "3.12",
              "python_full_version": "3.12.3"},
    "darwin": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix",
               "platform_machine": "arm64", "implementation_name": "cpython",
               "platform_python_implementation": "CPython", "python_version": "3.12",
               "python_full_version": "3.12.3"},
}
PLATFORM_MARKER = {"win32": 'sys_platform == "win32"', "linux": 'sys_platform == "linux"',
                   "darwin": 'sys_platform == "darwin"'}


def applies(marker: Marker | None, plat: str, extra: str) -> bool:
    if marker is None:
        return True
    env = dict(TARGETS[plat])
    env["extra"] = extra
    return marker.evaluate(env)


def closure(roots: list[Requirement]) -> dict[str, tuple[str | None, set[str], str]]:
    """{nombre: (versión instalada o None, plataformas, especificador declarado)}"""
    found: dict[str, tuple[str | None, set[str], str]] = {}
    queue: list[tuple[Requirement, set[str]]] = []
    for r in roots:
        plats = {p for p in TARGETS if applies(r.marker, p, "")}
        queue.append((r, plats))
    while queue:
        req, plats = queue.pop()
        name = canonicalize_name(req.name)
        if name in EXCLUDED or not plats:
            continue
        version, known_plats, spec = found.get(name, (None, set(), str(req.specifier)))
        new_plats = plats - known_plats
        try:
            dist = distribution(req.name)
            version = dist.version
        except PackageNotFoundError:
            dist = None
        found[name] = (version, known_plats | plats, spec or str(req.specifier))
        if dist is None:
            continue
        extras = set(req.extras) | {""}
        for raw in dist.requires or []:
            sub = Requirement(raw)
            sub_plats = {p for p in plats for e in extras if applies(sub.marker, p, e)}
            if not sub_plats:
                continue
            sub_name = canonicalize_name(sub.name)
            prev = found.get(sub_name, (None, set(), ""))[1]
            if new_plats or not sub_plats <= prev:
                queue.append((sub, sub_plats))
    return found


def _pypi_json(url: str) -> dict:  # type: ignore[type-arg]
    import httpx  # dependencia de [dev]; usa los certificados de certifi

    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return json.loads(r.text)


def latest_matching(name: str, spec: str) -> str:
    """Versión estable más reciente de PyPI que cumple el especificador declarado."""
    from packaging.specifiers import SpecifierSet

    releases = _pypi_json(f"{PYPI}/{name}/json")["releases"]
    ok: list[Version] = []
    for raw, files in releases.items():
        try:
            v = Version(raw)
        except InvalidVersion:
            continue
        if v.is_prerelease or not files or all(f.get("yanked") for f in files):
            continue
        if v in SpecifierSet(spec or ""):
            ok.append(v)
    if not ok:
        raise SystemExit(f"PyPI no tiene ninguna versión de {name} que cumpla {spec!r}")
    return str(max(ok))


def file_hashes(name: str, version: str) -> list[str]:
    data = _pypi_json(f"{PYPI}/{name}/{version}/json")
    hashes = sorted({f["digests"]["sha256"] for f in data["urls"] if not f.get("yanked")})
    if not hashes:
        raise SystemExit(f"PyPI no publica archivos de {name}=={version}")
    return hashes


def render(extra: str, found: dict[str, tuple[str | None, set[str], str]], *, hashes: bool = False) -> str:
    lines = [f"# Generado por «python -m tools.lock_requirements» — extra [{extra}] — Python 3.12",
             "# Instalar con: pip install --no-deps -r " + f"requirements-{extra}.txt",
             "# Excluidos a propósito: av (FFmpeg GPL), opencv-python (se usa opencv-python-headless)."]
    if hashes:
        lines.append("# Con SHA-256 de PyPI: pip comprueba cada archivo descargado (modo --require-hashes).")
    lines.append("")
    for name in sorted(found):
        version, plats, spec = found[name]
        marker = ""
        if plats != set(TARGETS):
            marker = " ; " + " or ".join(PLATFORM_MARKER[p] for p in sorted(plats))
        if hashes:
            version = version or latest_matching(name, spec)
            entry = [f"{name}=={version}{marker}"] + [f"--hash=sha256:{h}" for h in file_hashes(name, version)]
            lines.append(" \\\n    ".join(entry))
        elif version:
            lines.append(f"{name}=={version}{marker}")
        else:
            lines.append(f"{name}{spec}{marker}  # no instalado en esta plataforma: fijar al regenerar en destino")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.lock_requirements")
    parser.add_argument("--no-hashes", action="store_true", help="sin consultar PyPI (sin SHA-256)")
    args = parser.parse_args(argv)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base = [Requirement(r) for r in pyproject["project"]["dependencies"]]
    extras: dict[str, list[str]] = pyproject["project"]["optional-dependencies"]
    for extra, reqs in extras.items():
        roots = base + [Requirement(r) for r in reqs if canonicalize_name(Requirement(r).name) != SELF]
        for r in reqs:  # extras que referencian a otros extras del propio proyecto
            rr = Requirement(r)
            if canonicalize_name(rr.name) == SELF:
                for e in rr.extras:
                    roots += [Requirement(x) for x in extras[e]]
        found = closure(roots)
        out = ROOT / f"requirements-{extra}.txt"
        out.write_text(render(extra, found, hashes=extra in HASHED and not args.no_hashes), encoding="utf-8")
        print(f"{out.name}: {len(found)} paquetes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
