"""Publica un vídeo en bucle como flujo RTSP (para probar la analítica con «cámaras» reales).

    python -m tools.publish_video --video tests/assets/people-walking-h264.mp4 \\
        --url rtsp://127.0.0.1:8554/prueba/puerta

Si --url apunta a un MediaMTX propio, la ruta queda disponible para cualquier lector.
Para que se comporte como una cámara Hikvision/Dahua usa el simulador:
    python -m tools.camsim --hikvision hik1:2 --video 1=tests/assets/people-walking-h264.mp4
Por defecto copia el vídeo sin recodificar (-c copy); usa --encode si el archivo tiene B-frames
o no es H.264 (WebRTC en navegador exige H.264 sin B-frames).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def build_command(ffmpeg: str, video: Path, url: str, encode: bool, fps: int) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-stream_loop", "-1",
           "-i", str(video), "-an"]
    if encode:
        cmd += ["-vf", f"fps={fps}", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", str(fps * 2), "-bf", "0"]
    else:
        cmd += ["-c:v", "copy"]
    return cmd + ["-f", "rtsp", "-rtsp_transport", "tcp", url]


def main(argv: list[str] | None = None) -> int:
    from tools.camsim.simulator import find_ffmpeg

    p = argparse.ArgumentParser(description="Publica un vídeo en bucle por RTSP")
    p.add_argument("--video", type=Path, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--encode", action="store_true")
    p.add_argument("--fps", type=int, default=15)
    args = p.parse_args(argv)
    if not args.video.is_file():
        print(f"No existe el vídeo: {args.video} (ejecuta «python -m tools.download_test_assets»)", file=sys.stderr)
        return 2
    cmd = build_command(find_ffmpeg(), args.video, args.url, args.encode, args.fps)
    print("Publicando en bucle. Ctrl+C para terminar.")
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
