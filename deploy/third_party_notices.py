"""Genera THIRD_PARTY_NOTICES.txt con las licencias de todo lo que se redistribuye.

Uso (desde la raíz del repositorio, con el entorno de desarrollo instalado):
    .venv/bin/python -m deploy.third_party_notices            # escribe THIRD_PARTY_NOTICES.txt
    .venv/bin/python -m deploy.third_party_notices --check    # falla si el archivo no está al día

Fuentes:
  - Paquetes de Python: los archivos de bloqueo requirements-vms/analytics/central.txt (lo que
    se instala en los equipos del cliente). Licencia y textos se leen de los metadatos de cada
    paquete instalado en el entorno actual. Los que solo existen en otra plataforma (p. ej.
    pywin32-ctypes en Windows) llevan su licencia revisada a mano en EXTRA_PY.
  - Binarios que se descargan al instalar: MediaMTX, WinSW, Python embebible, pip.
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ["requirements-vms.txt", "requirements-analytics.txt", "requirements-central.txt"]
OUTPUT = ROOT / "THIRD_PARTY_NOTICES.txt"

# Paquetes que no están instalados en la máquina de desarrollo (dependen de la plataforma).
EXTRA_PY: dict[str, tuple[str, str]] = {
    "pywin32-ctypes": ("BSD-3-Clause", "https://github.com/enthought/pywin32-ctypes"),
    "jeepney": ("MIT", "https://gitlab.com/takluyver/jeepney"),
    "secretstorage": ("BSD-3-Clause", "https://github.com/mitya57/secretstorage"),
    "tzdata": ("Apache-2.0", "https://github.com/python/tzdata"),
    "colorama": ("BSD-3-Clause", "https://github.com/tartley/colorama"),
}

BINARIES = [
    ("MediaMTX v1.21.1", "MIT", "https://github.com/bluenviron/mediamtx",
     "Servidor de vídeo (RTSP, WebRTC, grabación). Binario oficial sin modificar; licencia en "
     "bin/MEDIAMTX-LICENSE.txt."),
    ("WinSW v2.12.0 (solo Windows)", "MIT", "https://github.com/winsw/winsw",
     "Envoltorio de servicios de Windows. Copyright (c) 2008-2020 Kohsuke Kawaguchi, Sun Microsystems, "
     "Inc., CloudBees, Inc., Oleg Nenashev and other contributors."),
    ("Python 3.12 embebible (solo Windows, si no hay Python en el equipo)", "PSF-2.0",
     "https://www.python.org/", "Distribución oficial de python.org sin modificar."),
    ("pip 26.2.1 (solo Windows embebible)", "MIT", "https://pip.pypa.io/", "Instalador de paquetes."),
    ("FFmpeg (incluido dentro de opencv-python-headless en Windows y Linux)", "LGPL-2.1-or-later",
     "https://ffmpeg.org/",
     "Biblioteca enlazada dinámicamente por OpenCV. Cumpliendo la LGPL: se usa sin modificar, se puede "
     "sustituir la biblioteca, y el código fuente correspondiente está disponible en "
     "https://github.com/opencv/opencv-python (scripts de compilación) y https://ffmpeg.org/download.html. "
     "Si lo solicitas, te lo facilitamos en un soporte físico por el coste de envío durante tres años."),
    ("libpq (incluida en psycopg-binary)", "PostgreSQL", "https://www.postgresql.org/", "Cliente de PostgreSQL."),
]

# Pesos de detección que se distribuyen en models/ (exportados a ONNX/OpenVINO desde el paquete rfdetr).
# Solo tamaños con licencia Apache-2.0 (nano/small/medium/base); los XL/2XL (PML) están prohibidos.
MODELS = [
    ("RF-DETR Nano y Small (pesos exportados a ONNX y OpenVINO en models/rfdetr-*.onnx|.xml|.bin)",
     "Apache-2.0", "https://github.com/roboflow/rf-detr",
     "Copyright (c) Roboflow, Inc. Pesos convertidos de formato (ONNX/OpenVINO) sin reentrenar; la conversión "
     "es una modificación de formato del trabajo original. Paquete de origen: rfdetr {rfdetr_version}. "
     "Texto completo de la licencia en la sección 3 («rfdetr»)."),
    ("DINOv2 (red troncal incluida en los pesos de RF-DETR)", "Apache-2.0",
     "https://github.com/facebookresearch/dinov2", "Copyright (c) Meta Platforms, Inc. and affiliates."),
    ("COCO (conjunto de datos con el que se entrenaron los pesos; no se distribuye)", "CC BY 4.0 (anotaciones)",
     "https://cocodataset.org/", "Solo se usa la clase «persona» del modelo preentrenado."),
]

LICENSE_FILE_RE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.IGNORECASE)


def locked_packages() -> list[str]:
    names: set[str] = set()
    for lock in LOCKS:
        path = ROOT / lock
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):   # «--hash=sha256:…» de la línea anterior
                continue
            m = re.match(r"^([A-Za-z0-9._\-]+)", line)
            if m:
                names.add(m.group(1).lower().replace("_", "-"))
    return sorted(names)


def license_of(meta: object) -> str:
    get = getattr(meta, "get")
    expr = get("License-Expression")
    if expr:
        return str(expr).strip()
    lic = (get("License") or "").strip()
    classifiers = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or [])  # type: ignore[attr-defined]
                   if c.startswith("License ::")]
    if lic and len(lic) < 80 and "\n" not in lic:
        return lic
    if classifiers:
        return " / ".join(classifiers)
    return lic.splitlines()[0][:80] if lic else "desconocida"


def build() -> str:
    out: list[str] = [
        "AVISOS DE TERCEROS — VMS Multimarca",
        "=" * 72,
        "",
        "VMS Multimarca incluye o descarga durante la instalación los componentes de código abierto",
        "que se listan a continuación, cada uno con su licencia. Ninguno tiene licencia copyleft fuerte",
        "(GPL/AGPL). Las bibliotecas LGPL se usan sin modificar y enlazadas dinámicamente.",
        "Archivo generado con «python -m deploy.third_party_notices»; no lo edites a mano.",
        "",
        "1. COMPONENTES BINARIOS",
        "-" * 72,
    ]
    for name, lic, url, note in BINARIES:
        out += [f"* {name}", f"  Licencia: {lic}", f"  Web: {url}", f"  {note}", ""]
    try:
        rfdetr_version = distribution("rfdetr").version
    except PackageNotFoundError:
        rfdetr_version = "1.11.2"
    out += ["1b. MODELOS DE DETECCIÓN DE PERSONAS", "-" * 72]
    for name, lic, url, note in MODELS:
        out += [f"* {name}", f"  Licencia: {lic}", f"  Web: {url}", f"  {note.format(rfdetr_version=rfdetr_version)}", ""]
    out += ["2. PAQUETES DE PYTHON", "-" * 72]
    texts: list[str] = []
    for name in locked_packages():
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            lic, url = EXTRA_PY.get(name, ("revisar en destino", f"https://pypi.org/project/{name}/"))
            out.append(f"* {name} (solo en otra plataforma) — {lic} — {url}")
            continue
        meta = dist.metadata
        url = meta.get("Home-page") or ""
        if not url:
            for entry in meta.get_all("Project-URL") or []:
                url = entry.split(",", 1)[-1].strip()
                break
        out.append(f"* {meta['Name']} {dist.version} — {license_of(meta)} — {url or 'https://pypi.org/project/' + name}")
        for f in sorted(dist.files or [], key=str):
            if LICENSE_FILE_RE.search(str(f)):
                try:
                    body = Path(str(dist.locate_file(f))).read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if body:
                    texts += ["", "=" * 72, f"{meta['Name']} {dist.version} — {f}", "=" * 72, body]
    try:
        rf = distribution("rfdetr")
        for f in sorted(rf.files or [], key=str):
            if LICENSE_FILE_RE.search(str(f)):
                body = Path(str(rf.locate_file(f))).read_text(encoding="utf-8", errors="replace").strip()
                texts += ["", "=" * 72, f"rfdetr {rf.version} (pesos RF-DETR en models/) — {f}", "=" * 72, body]
    except (PackageNotFoundError, OSError):
        texts += ["", "=" * 72, "rfdetr (pesos RF-DETR en models/)", "=" * 72,
                  "Apache License 2.0 — https://www.apache.org/licenses/LICENSE-2.0"]
    out += ["", "3. TEXTOS DE LICENCIA DE LOS PAQUETES DE PYTHON Y DE LOS MODELOS", "-" * 72]
    out += texts
    mtx = ROOT / "bin" / "MEDIAMTX-LICENSE.txt"
    if mtx.is_file():
        out += ["", "=" * 72, "MediaMTX — LICENSE", "=" * 72, mtx.read_text(encoding="utf-8").strip()]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Genera THIRD_PARTY_NOTICES.txt")
    p.add_argument("--check", action="store_true", help="solo comprueba que el archivo está al día")
    p.add_argument("--output", type=Path, default=OUTPUT)
    args = p.parse_args(argv)
    text = build()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != text:
            print(f"{args.output.name} no está al día: ejecuta «python -m deploy.third_party_notices»",
                  file=sys.stderr)
            return 1
        print(f"{args.output.name} al día")
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"Escrito {args.output} ({len(text) // 1024} KB, {len(locked_packages())} paquetes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
