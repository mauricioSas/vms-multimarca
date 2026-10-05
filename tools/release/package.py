"""Zips de componente reproducibles con `MANIFEST.sha256` (PLAN-V2 §2.7 y §4.1).

- Orden fijo (rutas POSIX ordenadas), fecha `SOURCE_DATE_EPOCH` (mínimo 1980), permisos normalizados
  (0644, o 0755 para `.exe`/`.dll`/`.pyd`/`.sh`), sin metadatos de usuario ni carpetas vacías.
- `MANIFEST.sha256` en la raíz del zip (formato `sha256sum`) con todos los archivos salvo él mismo.
- Las rutas del zip son relativas a `versions\\<X>\\`: el componente `app` aporta `app/`, `bin/` y
  `THIRD_PARTY_NOTICES.txt`; `runtime` aporta `runtime/`; etc. (`COMPONENT_ROOTS`). `updater` es el
  contenido de una ranura (`vmsctl.exe`, `runtime/`, `vms_updater/`).

Dos ejecuciones con la misma entrada y la misma `SOURCE_DATE_EPOCH` dan el mismo SHA-256.
"""
from __future__ import annotations

import hashlib
import os
import time
import zipfile
from pathlib import Path

from vms_updater.models import COMPONENT_ROOTS
from vms_updater.stage import MANIFEST_NAME, render_manifest

EXEC_SUFFIXES = {".exe", ".dll", ".pyd", ".sh"}
DEFAULT_EPOCH = 315532800       # 1980-01-01T00:00:00Z (lo mínimo que admite zip)


class PackageError(Exception):
    pass


def source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    try:
        return max(DEFAULT_EPOCH, int(raw)) if raw else DEFAULT_EPOCH
    except ValueError:
        return DEFAULT_EPOCH


def collect(src: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted(Path(src).rglob("*")):
        if p.is_symlink():
            raise PackageError(f"Enlace simbólico no permitido en el paquete: {p}")
        if p.is_file():
            rel = p.relative_to(src).as_posix()
            if rel == MANIFEST_NAME:
                continue
            out[rel] = p
    return out


def make_zip(src: Path, out: Path, *, component: str, epoch: int | None = None) -> str:
    """Empaqueta `src` en `out`. Devuelve el SHA-256 del zip."""
    files = collect(src)
    if not files:
        raise PackageError(f"{src} está vacío")
    roots = COMPONENT_ROOTS.get(component)
    if roots is not None:
        bad = sorted(r for r in files if r.split("/", 1)[0] not in roots)
        if bad:
            raise PackageError(f"«{component}» solo puede llevar {roots}; sobra {bad[:3]}")
    manifest = {rel: hashlib.sha256(p.read_bytes()).hexdigest() for rel, p in files.items()}
    date = time.gmtime(epoch if epoch is not None else source_date_epoch())[:6]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        def add(name: str, data: bytes, mode: int) -> None:
            info = zipfile.ZipInfo(name, date_time=date)  # type: ignore[arg-type]
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100000 | mode) << 16
            info.create_system = 3
            zf.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

        add(MANIFEST_NAME, render_manifest(manifest).encode("utf-8"), 0o644)
        for rel in sorted(files):
            mode = 0o755 if Path(rel).suffix.lower() in EXEC_SUFFIXES else 0o644
            add(rel, files[rel].read_bytes(), mode)
    os.replace(tmp, out)
    return hashlib.sha256(out.read_bytes()).hexdigest()
