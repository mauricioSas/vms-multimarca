"""SBOM CycloneDX 1.6 (JSON) de lo que se distribuye (PLAN-V2 §1.8, ``build.yml``).

Fuentes, sin dependencias nuevas:
- los locks de sede (``requirements-vms/analytics/central.txt``): paquetes Python del runtime;
- ``native/Cargo.lock``: crates de ``vmshost``/``vmsctl`` (y del visor cuando B2 lo añada a ``native/``);
- MediaMTX (versión fijada en ``tools/fetch_mediamtx.py``) y el Python embebible.

La licencia se toma de los metadatos instalados cuando el paquete está en el entorno de build (en CI lo
está: se instalan los mismos locks); si no, se omite (CycloneDX lo permite). El documento es determinista:
mismo ``serialNumber`` (UUID v5 de versión + fecha) y la marca de tiempo es ``SOURCE_DATE_EPOCH``.
"""
from __future__ import annotations

import json
import re
import time
import tomllib
import uuid
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from . import ROOT

LOCKS = ("requirements-vms.txt", "requirements-analytics.txt", "requirements-central.txt")
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
_NAMESPACE = uuid.UUID("6f1c9a0e-5c1d-4d3e-9a7b-2f3c4d5e6f70")


def _pypi_components(root: Path) -> list[dict[str, object]]:
    seen: dict[str, dict[str, object]] = {}
    for lock in LOCKS:
        path = root / lock
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _REQ.match(line.strip())
            if not m:
                continue
            name, version = m.group(1).lower().replace("_", "-"), m.group(2)
            comp: dict[str, object] = {"type": "library", "name": name, "version": version,
                                       "purl": f"pkg:pypi/{name}@{version}", "bom-ref": f"pypi:{name}@{version}"}
            lic = _installed_license(name, version)
            if lic:
                comp["licenses"] = [{"expression": lic}] if " " in lic else [{"license": {"id": lic}}]
            seen[str(comp["bom-ref"])] = comp
    return [seen[k] for k in sorted(seen)]


def _installed_license(name: str, version: str) -> str | None:
    try:
        dist = distribution(name)
    except PackageNotFoundError:
        return None
    if dist.version != version:
        return None
    expr = dist.metadata.get("License-Expression")
    if expr:
        return str(expr).strip()
    return None


def _cargo_components(root: Path) -> list[dict[str, object]]:
    lock = root / "native" / "Cargo.lock"
    if not lock.is_file():
        return []
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    out = []
    for pkg in data.get("package", []):
        if "source" not in pkg:      # nuestros crates (vmshost, vmsctl, vms-common)
            continue
        name, version = pkg["name"], pkg["version"]
        out.append({"type": "library", "name": name, "version": version, "purl": f"pkg:cargo/{name}@{version}",
                    "bom-ref": f"cargo:{name}@{version}"})
    return sorted(out, key=lambda c: str(c["bom-ref"]))


def _binary_components() -> list[dict[str, object]]:
    try:
        from tools.fetch_mediamtx import VERSION as MTX
    except ImportError:  # pragma: no cover
        MTX = "v0.0.0"
    return [
        {"type": "application", "name": "mediamtx", "version": MTX.lstrip("v"),
         "purl": f"pkg:github/bluenviron/mediamtx@{MTX}", "bom-ref": f"github:mediamtx@{MTX}",
         "licenses": [{"license": {"id": "MIT"}}]},
        {"type": "application", "name": "python-embed-amd64", "version": "3.12",
         "bom-ref": "python:embed-3.12", "licenses": [{"license": {"id": "PSF-2.0"}}]},
    ]


def build_sbom(version: str, epoch: int, root: Path = ROOT) -> dict[str, object]:
    components = _pypi_components(root) + _cargo_components(root) + _binary_components()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(_NAMESPACE, f'vms-multimarca:{version}:{epoch}')}",
        "version": 1,
        "metadata": {
            "timestamp": stamp,
            "component": {"type": "application", "name": "vms-multimarca", "version": version,
                          "bom-ref": "vms-multimarca"},
            "supplier": {"name": "Unmanned Studio"},
        },
        "components": components,
        "dependencies": [{"ref": "vms-multimarca", "dependsOn": [str(c["bom-ref"]) for c in components]}],
    }


def write_sbom(dest: Path, version: str, epoch: int, root: Path = ROOT) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(build_sbom(version, epoch, root), indent=1, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return dest
