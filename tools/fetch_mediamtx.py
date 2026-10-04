"""Descarga el binario oficial de MediaMTX (licencia MIT) y comprueba su SHA-256.

Uso:
    python -m tools.fetch_mediamtx                       # plataforma actual → ./bin/
    python -m tools.fetch_mediamtx --platform windows_amd64 --dest build/bin
    python -m tools.fetch_mediamtx --archive ruta/al/mediamtx_vX_os_arch.tar.gz   # sin red

El checksum se valida contra checksums.sha256 publicado en la misma release. Junto al
binario se deja LICENSE (obligatorio al redistribuir).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import platform
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx

VERSION = "v1.21.1"
BASE = "https://github.com/bluenviron/mediamtx/releases/download"
ROOT = Path(__file__).resolve().parents[1]


def current_platform() -> str:
    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        raise SystemExit(f"Arquitectura no soportada: {machine}")
    return f"{system}_{arch}"


def asset_name(version: str, plat: str) -> str:
    ext = "zip" if plat.startswith("windows") else "tar.gz"
    return f"mediamtx_{version}_{plat}.{ext}"


def expected_sha256(client: httpx.Client, version: str, name: str) -> str:
    r = client.get(f"{BASE}/{version}/checksums.sha256")
    r.raise_for_status()
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise SystemExit(f"{name} no aparece en checksums.sha256")


def extract(data: bytes, name: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    exe = "mediamtx.exe" if "windows" in name else "mediamtx"
    wanted = {exe, "LICENSE"}
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if Path(member).name in wanted:
                    (dest / Path(member).name).write_bytes(zf.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile() and Path(member.name).name in wanted:
                    f = tf.extractfile(member)
                    assert f is not None
                    (dest / Path(member.name).name).write_bytes(f.read())
    target = dest / exe
    if not target.is_file():
        raise SystemExit("El archivo descargado no contiene el ejecutable de MediaMTX")
    target.chmod(0o755)
    lic = dest / "LICENSE"
    if lic.exists():
        lic.replace(dest / "MEDIAMTX-LICENSE.txt")
    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Descarga MediaMTX con verificación SHA-256")
    p.add_argument("--version", default=VERSION)
    p.add_argument("--platform", default=None, help="p. ej. windows_amd64, linux_amd64, darwin_arm64")
    p.add_argument("--dest", type=Path, default=ROOT / "bin")
    p.add_argument("--archive", type=Path, help="Usar un archivo ya descargado (se verifica igual)")
    args = p.parse_args(argv)
    plat = args.platform or current_platform()
    name = asset_name(args.version, plat)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        sha = expected_sha256(client, args.version, name)
        if args.archive:
            data = args.archive.read_bytes()
        else:
            print(f"Descargando {name}...")
            r = client.get(f"{BASE}/{args.version}/{name}")
            r.raise_for_status()
            data = r.content
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        print(f"SHA-256 no coincide: esperado {sha}, obtenido {got}", file=sys.stderr)
        return 1
    target = extract(data, name, args.dest)
    print(f"MediaMTX {args.version} ({plat}) verificado → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
