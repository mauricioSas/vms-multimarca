"""Los archivos de datos de `vms/ops` (tabla de avisos y plantilla del visor) tienen que ir en el paquete
instalado: sin ellos la auditoría de seguridad responde 503 y toda exportación de evidencias falla.

`pyproject.toml` es del arquitecto: la línea `"vms.ops" = ["security/*.json", "evidence/templates/*.html"]` de
`[tool.setuptools.package-data]` está pedida. Mientras no esté, esta prueba queda como «xfail» (visible en el
resumen de pytest) en lugar de poner la suite en rojo; cuando se aplique, pasará sola.
"""
from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "vms" / "ops"


def _ops_data_files() -> list[str]:
    return sorted(p.relative_to(OPS).as_posix() for p in OPS.rglob("*")
                  if p.is_file() and p.suffix not in (".py", ".pyc", ".md") and "__pycache__" not in p.parts)


def test_ops_has_the_expected_data_files() -> None:
    files = _ops_data_files()
    assert "security/advisories.json" in files and "evidence/templates/visor.html" in files, files


def _missing_from_package_data() -> list[str]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    globs = cfg.get("tool", {}).get("setuptools", {}).get("package-data", {}).get("vms.ops", [])
    return [f for f in _ops_data_files() if not any(fnmatch.fnmatch(f, g) for g in globs)]


_MISSING = _missing_from_package_data()


@pytest.mark.xfail(bool(_MISSING), reason="Petición pendiente al arquitecto: package-data de vms.ops en pyproject.toml "
                                          f"(faltan {', '.join(_MISSING)})", strict=False)
def test_ops_data_files_are_in_package_data() -> None:
    assert _missing_from_package_data() == []
