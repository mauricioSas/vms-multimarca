"""Monta `versions\\<X>\\` a partir de los componentes (PLAN-V2 §2.5 paso 3 y §2.7).

- Cada zip de componente lleva `MANIFEST.sha256` (formato de `sha256sum`: `<hash>  <ruta>`). Tras
  descomprimir se comprueba **todo**: cada archivo listado existe con su hash y no sobra ninguno. Es
  defensa en profundidad frente a zips manipulados en disco (el zip ya viene verificado por TUF).
- Se rechazan rutas absolutas, `..`, enlaces simbólicos, nombres repetidos y archivos fuera de las
  carpetas del componente (`COMPONENT_ROOTS`).
- Lo que no cambia respecto a la versión activa se copia con **enlaces duros** (ocupa disco una vez).
- Se monta en `versions\\<X>.tmp\\` y se renombra al final: una carpeta `versions\\<X>\\` siempre está
  completa. Si algo falla (p. ej. disco lleno), se borra el `.tmp` y se reintenta en el siguiente ciclo.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from .models import COMPONENT_ROOTS, ReleaseDescriptor

log = logging.getLogger("vms_updater.stage")

MANIFEST_NAME = "MANIFEST.sha256"
MAX_UNCOMPRESSED = 8 * 1024 ** 3       # 8 GB: nada del producto se acerca
MAX_ENTRIES = 200_000


def _copy_stream(src: object, dst: object, length: int = 1024 * 1024) -> None:
    """Copia de un miembro del zip a disco (punto único para simular «disco lleno» en las pruebas)."""
    shutil.copyfileobj(src, dst, length)  # type: ignore[arg-type]


class StageError(Exception):
    def __init__(self, message_es: str) -> None:
        super().__init__(message_es)
        self.message_es = message_es


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def parse_manifest(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise StageError(f"{MANIFEST_NAME}: línea {n} no válida")
        rel = rel.strip()
        _check_rel(rel)
        if rel in out:
            raise StageError(f"{MANIFEST_NAME}: «{rel}» repetido")
        out[rel] = digest
    return out


def render_manifest(entries: dict[str, str]) -> str:
    return "".join(f"{entries[k]}  {k}\n" for k in sorted(entries))


def _check_rel(rel: str) -> None:
    p = PurePosixPath(rel)
    if not rel or rel.startswith("/") or "\\" in rel or p.is_absolute() or ".." in p.parts or ":" in p.parts[0]:
        raise StageError(f"ruta no permitida en el paquete: {rel!r}")


def _allowed(rel: str, roots: tuple[str, ...] | None) -> bool:
    if roots is None:
        return True
    first = PurePosixPath(rel).parts[0]
    return first in roots


def extract_component(zip_path: Path, dest: Path, *, roots: tuple[str, ...] | None,
                      copy: Callable[..., object] | None = None) -> dict[str, str]:
    """Descomprime en `dest` (vacía) y comprueba el manifiesto. Devuelve {ruta: sha256}."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise StageError(f"{zip_path.name} no es un zip válido: {exc}") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise StageError(f"{zip_path.name}: demasiadas entradas")
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
            raise StageError(f"{zip_path.name}: tamaño descomprimido excesivo")
        names: set[str] = set()
        manifest_text: str | None = None
        for info in infos:
            name = info.filename
            if name in names:
                raise StageError(f"{zip_path.name}: «{name}» repetido")
            names.add(name)
            if info.is_dir():
                _check_rel(name.rstrip("/"))
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise StageError(f"{zip_path.name}: enlace simbólico no permitido ({name})")
            if name == MANIFEST_NAME:
                manifest_text = zf.read(info).decode("utf-8")
                continue
            _check_rel(name)
            if not _allowed(name, roots):
                raise StageError(f"{zip_path.name}: «{name}» está fuera de las carpetas del componente")
            target = dest.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                (copy or _copy_stream)(src, out, 1024 * 1024)
        if manifest_text is None:
            raise StageError(f"{zip_path.name} no trae {MANIFEST_NAME}")
    manifest = parse_manifest(manifest_text)
    verify_tree(dest, manifest, label=zip_path.name)
    return manifest


def list_files(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(root).as_posix()] = p
    return out


def verify_tree(root: Path, manifest: dict[str, str], *, label: str, subset: Iterable[str] | None = None) -> None:
    files = list_files(root)
    expected = set(manifest) if subset is None else set(subset)
    present = set(files) if subset is None else {k for k in files if k in expected}
    missing = sorted(expected - present)
    extra = sorted(set(files) - set(manifest)) if subset is None else []
    if missing:
        raise StageError(f"{label}: faltan archivos del manifiesto ({missing[:3]}…)")
    if extra:
        raise StageError(f"{label}: archivos que no están en el manifiesto ({extra[:3]}…)")
    for rel in sorted(expected):
        if sha256_file(files[rel]) != manifest[rel]:
            raise StageError(f"{label}: «{rel}» no coincide con su hash")


def link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def make_readonly(root: Path) -> None:
    """Las carpetas de `versions\\` son inmutables (en Windows, el ACL lo pone `vmsctl acl apply`)."""
    for p in root.rglob("*"):
        if p.is_file():
            try:
                p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass


def _make_writable(func: Callable[..., object], path: str, _exc: object) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(path) else 0))
        func(path)
    except OSError:
        pass


def remove_tree(path: Path) -> bool:
    """Borra una carpeta aunque tenga archivos de solo lectura. True si ya no existe."""
    if not path.exists():
        return True
    shutil.rmtree(path, onexc=_make_writable)
    return not path.exists()


def stage_version(*, versions_dir: Path, descriptor: ReleaseDescriptor, descriptor_bytes: bytes,
                  zips: dict[str, Path], current_version: str | None,
                  current_descriptor: ReleaseDescriptor | None,
                  copy: Callable[..., object] | None = None) -> Path:
    """Deja `versions\\<X>\\` completa. `zips` = componentes que cambian (ya verificados por TUF)."""
    final = versions_dir / descriptor.version
    if final.is_dir() and (final / "release.json").is_file():
        if (final / "release.json").read_bytes() == descriptor_bytes:
            log.info("versions/%s ya está montada", descriptor.version)
            return final
        raise StageError(f"versions/{descriptor.version} existe con otro descriptor: no se toca")
    tmp = versions_dir / f"{descriptor.version}.tmp"
    remove_tree(tmp)
    tmp.mkdir(parents=True)
    current_dir = versions_dir / current_version if current_version else None
    try:
        manifests_dir = tmp / "manifests"
        manifests_dir.mkdir()
        for comp, ref in descriptor.components.items():
            if comp == "updater":
                continue
            roots = COMPONENT_ROOTS[comp]
            if comp in zips:
                stage = tmp / ".stage" / comp
                manifest = extract_component(zips[comp], stage, roots=roots, copy=copy)
                for top in sorted({PurePosixPath(k).parts[0] for k in manifest}):
                    os.replace(stage / top, tmp / top)
            else:
                # sin cambios: enlaces duros desde la versión activa, comprobados con su manifiesto
                cur_ref = current_descriptor.components.get(comp) if current_descriptor else None
                if current_dir is None or cur_ref is None or cur_ref.sha256 != ref.sha256:
                    raise StageError(f"falta el componente «{comp}» y no está en la versión activa")
                cur_manifest_file = current_dir / "manifests" / f"{comp}.sha256"
                if not cur_manifest_file.is_file():
                    raise StageError(f"la versión activa no tiene el manifiesto de «{comp}»")
                manifest = parse_manifest(cur_manifest_file.read_text(encoding="utf-8"))
                for rel in manifest:
                    link_or_copy(current_dir.joinpath(*PurePosixPath(rel).parts), tmp.joinpath(*PurePosixPath(rel).parts))
                verify_tree(tmp, manifest, label=f"{comp} (enlazado)", subset=manifest.keys())
            (manifests_dir / f"{comp}.sha256").write_text(render_manifest(manifest), encoding="utf-8")
        remove_tree(tmp / ".stage")
        (tmp / "release.json").write_bytes(descriptor_bytes)
        make_readonly(tmp)
        os.replace(tmp, final)
    except BaseException:
        remove_tree(tmp)
        raise
    return final


def install_updater_slot(*, slot_dir: Path, zip_path: Path, copy: Callable[..., object] | None = None) -> None:
    """Escribe una ranura del actualizador (`updater\\slot-x\\`) completa o nada."""
    tmp = slot_dir.with_name(slot_dir.name + ".tmp")
    remove_tree(tmp)
    try:
        extract_component(zip_path, tmp, roots=None, copy=copy)
        old = slot_dir.with_name(slot_dir.name + ".old")
        remove_tree(old)
        if slot_dir.exists():
            os.replace(slot_dir, old)
        os.replace(tmp, slot_dir)
        remove_tree(old)
    except BaseException:
        remove_tree(tmp)
        raise
