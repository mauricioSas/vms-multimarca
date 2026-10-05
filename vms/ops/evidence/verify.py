"""Verificación de un paquete de evidencias (CONTRATO §18.7): `python -m vms.ops.evidence verify <zip|carpeta>`.

Comprueba, sin red:
1. que `manifiesto.json` es un `EvidenceManifest` válido;
2. la firma Ed25519 de `manifiesto.sig` sobre los bytes exactos del manifiesto, con la clave del propio
   manifiesto, y que `clave-publica.pem` y `key_id` corresponden a esa clave;
3. el tamaño y el SHA-256 de CADA archivo listado, y que no sobra ninguno.

La clave pública viaja DENTRO del paquete: quien falsifique un paquete puede re-firmarlo con otra clave y
el paquete seguirá siendo «coherente». Por eso la firma solo prueba el origen si el `key_id` coincide con el
de la instalación que lo exportó, que la central conoce por el latido (`payload.evidence_key`) y que figura en
el acta original. `--key-id <hex>` hace esa comprobación (`VerifyResult.key_checked`).

Códigos de salida del CLI: 0 = todo cuadra y la clave es la esperada; 3 = huellas y firma coherentes pero la
clave NO se ha comprobado (falta `--key-id`); 1 = algo no coincide; 2 = no se puede leer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from ..models import EvidenceManifest
from .export import MANIFEST, PUBKEY, SIGNATURE
from .keys import key_id_of

CHUNK = 1024 * 1024


@dataclass
class VerifyResult:
    ok: bool = True
    manifest: EvidenceManifest | None = None
    errors: list[str] = field(default_factory=list)
    checked_files: int = 0
    key_checked: bool = False         # el key_id de la firma coincide con el esperado (--key-id)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)


class _Source(Protocol):
    def names(self) -> list[str]: ...
    def read(self, name: str) -> bytes: ...
    def open(self, name: str) -> IO[bytes]: ...
    def size(self, name: str) -> int: ...


class _ZipSource:
    def __init__(self, path: Path) -> None:
        self.zf = zipfile.ZipFile(path)
        names = [n for n in self.zf.namelist() if not n.endswith("/")]
        # Dos entradas con el mismo nombre (o que solo cambian en mayúsculas, que en Windows son el mismo archivo):
        # zipfile lee la ÚLTIMA y el Explorador o 7-Zip pueden enseñar la primera. Un paquete así no se verifica.
        seen: set[str] = set()
        for n in names:
            key = n.replace("\\", "/").casefold()
            if key in seen:
                raise ValueError(f"El paquete tiene archivos repetidos con el mismo nombre ({n}): está manipulado")
            seen.add(key)
        manifests = [n for n in names if n == MANIFEST or n.endswith("/" + MANIFEST)]
        if len(manifests) != 1:
            raise ValueError("El paquete no tiene exactamente un manifiesto.json")
        self.prefix = manifests[0][: -len(MANIFEST)]
        self._names = [n[len(self.prefix):] for n in names if n.startswith(self.prefix)]
        self._outside = [n for n in names if not n.startswith(self.prefix)]

    def names(self) -> list[str]:
        return self._names + [f"../{n}" for n in self._outside]

    def read(self, name: str) -> bytes:
        return self.zf.read(self.prefix + name)

    def open(self, name: str) -> IO[bytes]:
        return self.zf.open(self.prefix + name)

    def size(self, name: str) -> int:
        return self.zf.getinfo(self.prefix + name).file_size


class _DirSource:
    def __init__(self, path: Path) -> None:
        self.root = path
        if not (path / MANIFEST).is_file():
            subs = [p for p in path.iterdir() if p.is_dir() and (p / MANIFEST).is_file()]
            if len(subs) != 1:
                raise ValueError("En esa carpeta no está manifiesto.json")
            self.root = subs[0]

    def names(self) -> list[str]:
        return [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]

    def read(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def open(self, name: str) -> IO[bytes]:
        return open(self.root / name, "rb")

    def size(self, name: str) -> int:
        return (self.root / name).stat().st_size


def _safe(name: str) -> bool:
    return bool(name) and not name.startswith(("/", "\\")) and ".." not in name.replace("\\", "/").split("/")


def verify(path: str | Path, *, expect_key_id: str | None = None) -> VerifyResult:
    res = VerifyResult()
    p = Path(path)
    try:
        src: _Source = _ZipSource(p) if p.is_file() else _DirSource(p)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        res.fail(f"No se pudo abrir el paquete: {exc}")
        return res
    try:
        raw = src.read(MANIFEST)
        manifest = EvidenceManifest.model_validate(json.loads(raw))
    except (KeyError, OSError, ValueError, ValidationError) as exc:
        res.fail(f"manifiesto.json no es válido: {exc}")
        return res
    res.manifest = manifest
    # --- firma
    try:
        pub_raw = base64.b64decode(manifest.signing_key.get("public_key", ""), validate=True)
        pub = Ed25519PublicKey.from_public_bytes(pub_raw)
        sig = base64.b64decode(src.read(SIGNATURE).strip(), validate=True)
        pub.verify(sig, raw)
    except InvalidSignature:
        res.fail("La firma de manifiesto.json NO es válida: el manifiesto se ha modificado o no es de esta clave")
    except (KeyError, OSError, ValueError) as exc:
        res.fail(f"No se pudo comprobar la firma: {exc}")
        pub_raw = b""
    if pub_raw:
        kid = key_id_of(pub_raw)
        if manifest.signing_key.get("key_id") != kid:
            res.fail("El key_id del manifiesto no corresponde a su clave pública")
        if expect_key_id:
            if expect_key_id.strip().lower() != kid:
                res.fail(f"La firma es de otra instalación (key_id {kid[:16]}…, se esperaba "
                         f"{expect_key_id.strip()[:16]}…)")
            else:
                res.key_checked = True
        try:
            pem = serialization.load_pem_public_key(src.read(PUBKEY))
            if not isinstance(pem, Ed25519PublicKey) or pem.public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw) != pub_raw:
                res.fail("clave-publica.pem no coincide con la clave del manifiesto")
        except (KeyError, OSError, ValueError) as exc:
            res.fail(f"clave-publica.pem ilegible: {exc}")
    # --- archivos
    listed = {f.path for f in manifest.files}
    present = set(src.names())
    for f in manifest.files:
        if not _safe(f.path):
            res.fail(f"Ruta no permitida en el manifiesto: {f.path}")
            continue
        if f.path not in present:
            res.fail(f"Falta el archivo {f.path}")
            continue
        if src.size(f.path) != f.bytes:
            res.fail(f"{f.path}: el tamaño no coincide ({src.size(f.path)} frente a {f.bytes} bytes)")
            continue
        h = hashlib.sha256()
        with src.open(f.path) as fh:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
        if h.hexdigest() != f.sha256:
            res.fail(f"{f.path}: el SHA-256 no coincide (el archivo se ha modificado)")
        res.checked_files += 1
    extra = sorted(present - listed - {MANIFEST, SIGNATURE})
    for name in extra:
        res.fail(f"Archivo que no está en el manifiesto: {name}")
    if not res.ok:
        res.key_checked = False
    return res
