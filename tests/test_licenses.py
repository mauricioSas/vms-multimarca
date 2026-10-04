"""Guardia de licencias: el entorno no debe contener nada prohibido (docs/CONTRATO.md §10)."""
from __future__ import annotations

import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution, distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PACKAGES = {
    "av": "PyAV: sus wheels traen FFmpeg GPL",
    "ultralytics": "AGPL-3.0",
    "python-amcrest": "GPL",
    "amcrest": "GPL",
    "imageio-ffmpeg": "trae un ffmpeg GPL",
    "pyside6": "solo se permitiría con enlace dinámico; el producto no lo usa",
    "pyqt5": "GPL", "pyqt6": "GPL",
    "python-mpv": "GPL/LGPL con DLL de terceros",
    "opencv-python": "usar opencv-python-headless",
}
# Licencias copyleft fuerte: no se aceptan en el entorno del producto.
GPL_RE = re.compile(r"\b(A?GPL|GNU (Affero )?General Public License)\b", re.IGNORECASE)
# Excepciones revisadas a mano (nombre → motivo).
ALLOWED_EXCEPTIONS: dict[str, str] = {
    "matplotlib": "Licencia PSF; incluye FreeType con licencia dual «FTL OR GPL-2.0-or-later»: se acoge a FTL",
    "scipy": "BSD-3; la wheel incluye libgfortran «GPL-3.0-or-later WITH GCC-exception-3.1» (la excepción de "
             "runtime de GCC permite enlazarla desde software propietario) y libquadmath LGPL",
}


def _license_text(dist_name: str) -> str:
    meta = distribution(dist_name).metadata
    parts = [meta.get("License-Expression") or "", meta.get("License") or ""]
    parts += [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return " | ".join(p for p in parts if p)


def test_forbidden_packages_not_installed() -> None:
    found = {}
    for name, reason in FORBIDDEN_PACKAGES.items():
        try:
            distribution(name)
            found[name] = reason
        except PackageNotFoundError:
            pass
    assert not found, f"Paquetes prohibidos instalados: {found}"


def test_no_gpl_licensed_distributions() -> None:
    offenders = {}
    for d in distributions():
        name = d.metadata["Name"]
        if name.lower() in ALLOWED_EXCEPTIONS:
            continue
        text = _license_text(name)
        # LGPL está permitido con enlace dinámico; se excluye antes de buscar GPL.
        stripped = re.sub(r"\bLGPL[\w.\-+]*|Lesser General Public License", "", text, flags=re.IGNORECASE)
        if GPL_RE.search(stripped):
            offenders[name] = text[:120]
    assert not offenders, f"Distribuciones con licencia GPL/AGPL: {offenders}"


def test_no_forbidden_imports_in_product_code() -> None:
    pattern = re.compile(r"^\s*(import|from)\s+(av|ultralytics|amcrest|PySide6|imageio_ffmpeg|mpv)\b", re.M)
    hits = []
    for pkg in ("vms", "analytics", "central"):
        for f in (ROOT / pkg).rglob("*.py"):
            if pattern.search(f.read_text(encoding="utf-8")):
                hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"Imports prohibidos en el producto: {hits}"


def test_rfdetr_only_apache_sizes_referenced() -> None:
    hits = []
    for pkg in ("vms", "analytics", "central"):
        for f in (ROOT / pkg).rglob("*.py"):
            if re.search(r"RFDETR(XLarge|2XLarge|XL)\b|rf-detr-(xl|2xl)", f.read_text(encoding="utf-8"), re.I):
                hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"Referencias a pesos RF-DETR con licencia PML: {hits}"


@pytest.mark.skipif(sys.platform == "darwin",
                    reason="La wheel macOS arm64 de OpenCV trae FFmpeg de Homebrew compilado con --enable-gpl; "
                           "macOS solo se usa para desarrollo, nunca se distribuye")
def test_opencv_ffmpeg_is_lgpl_on_target_platforms() -> None:
    import cv2

    base = Path(cv2.__file__).parent
    candidates = list(base.glob("opencv_videoio_ffmpeg*.dll")) + list(base.parent.glob("opencv_python*.libs/libavcodec*"))
    assert candidates, "No se encontró el FFmpeg incluido en OpenCV"
    for lib in candidates:
        data = lib.read_bytes()
        assert b"--enable-gpl" not in data, f"{lib.name} está compilado con --enable-gpl"
        assert b"libx264" not in data.lower() or b"license: LGPL" in data, f"{lib.name} parece incluir x264"


def test_pip_licenses_report_runs() -> None:
    """Deja un informe de licencias legible (pip-licenses) para revisión humana."""
    out = subprocess.run([sys.executable, "-m", "piplicenses", "--format=csv", "--with-urls"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "fastapi" in out.stdout.lower()
