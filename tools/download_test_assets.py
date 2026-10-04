"""Descarga los vídeos de prueba de la analítica a tests/assets/ (carpeta ignorada por git).

    python -m tools.download_test_assets

- people-walking.mp4: personas caminando en plano cenital-oblicuo (catálogo público de
  supervision, verificado por MD5). Solo para pruebas internas: no se distribuye ni se
  incluye en el instalador.
- Además genera people-walking-h264.mp4: misma escena recodificada a H.264 baseline sin
  B-frames, 1280 px de ancho, GOP 2 s (lo que entregaría un subflujo de cámara real).
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tests" / "assets"

VIDEOS = {
    "people-walking.mp4": ("https://media.roboflow.com/supervision/video-examples/people-walking.mp4",
                           "0574c053c8686c3f1dc0aa3743e45cb9"),
}


def md5_of(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(name: str, url: str, md5: str) -> Path:
    target = ASSETS / name
    if target.is_file() and md5_of(target) == md5:
        print(f"Ya existe y es correcto: {target}")
        return target
    tmp = target.with_suffix(".part")
    print(f"Descargando {url} ...")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    got = md5_of(tmp)
    if got != md5:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"MD5 no coincide para {name}: esperado {md5}, obtenido {got}")
    tmp.replace(target)
    print(f"OK: {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


def transcode_h264(src: Path, ffmpeg: str) -> Path:
    dst = src.with_name(src.stem + "-h264.mp4")
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-an",
           "-vf", "scale=1280:-2,fps=15", "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "baseline",
           "-pix_fmt", "yuv420p", "-g", "30", "-bf", "0", "-movflags", "+faststart", str(dst)]
    subprocess.run(cmd, check=True)
    print(f"OK: {dst}")
    return dst


def main() -> int:
    from tools.camsim.simulator import find_ffmpeg

    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, (url, md5) in VIDEOS.items():
        src = download(name, url, md5)
        try:
            transcode_h264(src, find_ffmpeg())
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"Aviso: no se pudo recodificar ({exc}); se usará el original", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
