"""Simulador de cámaras en línea de comandos.

Ejemplos:
    python -m tools.camsim --hikvision hik1:4 --dahua dah1:4
    python -m tools.camsim --hikvision hik1:2 --video 2=tests/assets/people-walking.mp4 --base-port 18600

Cada equipo recibe un puerto (consecutivos desde --base-port, o libres si es 0). Una vez
arrancado acepta órdenes por teclado:
    kill <equipo> <canal> <main|sub>     mata el ffmpeg de ese flujo
    start <equipo> <canal> <main|sub>    lo relanza
    off <equipo> / on <equipo>           desenchufa / enchufa el equipo entero
    urls                                 vuelve a mostrar las URLs
    quit                                 termina
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice, install_sigterm_handler


def _parse_dev(spec: str, vendor: str) -> SimDevice:
    name, _, n = spec.partition(":")
    if not name.isalnum() or not name.islower():
        raise argparse.ArgumentTypeError(f"Nombre de equipo no válido: {name!r} (minúsculas y números)")
    return SimDevice(name=name, vendor=vendor, channels=int(n or 1))  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.camsim", description="Simulador de cámaras Hikvision/Dahua")
    p.add_argument("--hikvision", action="append", default=[], metavar="NOMBRE:CANALES")
    p.add_argument("--dahua", action="append", default=[], metavar="NOMBRE:CANALES")
    p.add_argument("--generic", action="append", default=[], metavar="NOMBRE:CANALES")
    p.add_argument("--video", action="append", default=[], metavar="CANAL=ARCHIVO",
                   help="Usa un vídeo en bucle para ese canal en todos los equipos")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    p.add_argument("--auth", choices=["digest", "basic", "none"], default="digest")
    p.add_argument("--base-port", type=int, default=18600, help="0 = puertos libres aleatorios")
    p.add_argument("--workdir", type=Path, default=Path(".tmp/camsim"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    devices = ([_parse_dev(s, "hikvision") for s in args.hikvision] + [_parse_dev(s, "dahua") for s in args.dahua]
               + [_parse_dev(s, "generic") for s in args.generic])
    if not devices:
        devices = [SimDevice("hik1", "hikvision", channels=2), SimDevice("dah1", "dahua", channels=2)]
    videos: dict[int, Path] = {}
    for v in args.video:
        ch, _, f = v.partition("=")
        videos[int(ch)] = Path(f).resolve()
    for i, d in enumerate(devices):
        d.username, d.password, d.auth, d.videos = args.user, args.password, args.auth, dict(videos)
        if args.base_port:
            d.port = args.base_port + 1 + i
    sim = CameraSimulator(devices, args.workdir, rtsp_port=args.base_port or 0)
    install_sigterm_handler(sim)
    print("Arrancando simulador...", flush=True)
    sim.start()

    def show() -> None:
        print(f"MediaMTX interno: rtsp://127.0.0.1:{sim.rtsp_port}  API: http://127.0.0.1:{sim.api_port}")
        for d in sim.devices.values():
            print(f"[{d.name}] {d.vendor} puerto {d.port} usuario {d.username!r} auth {d.auth}")
            for ch in range(1, d.channels + 1):
                for s in ("main", "sub"):
                    print(f"   ch{ch} {s:4} {d.rtsp_url(ch, s)}")  # type: ignore[arg-type]
        sys.stdout.flush()

    show()
    try:
        for line in sys.stdin:
            parts = line.split()
            if not parts:
                continue
            cmd = parts[0].lower()
            try:
                if cmd in ("quit", "exit", "q"):
                    break
                if cmd == "urls":
                    show()
                elif cmd in ("kill", "start") and len(parts) == 4:
                    dev, ch, stream = parts[1], int(parts[2]), parts[3]
                    (sim.kill_stream if cmd == "kill" else sim.start_stream)(dev, ch, stream)  # type: ignore[operator]
                    print(f"ok: {cmd} {dev} ch{ch} {stream}", flush=True)
                elif cmd in ("off", "on") and len(parts) == 2:
                    sim.set_device_online(parts[1], cmd == "on")
                    print(f"ok: {parts[1]} {'enchufado' if cmd == 'on' else 'desenchufado'}", flush=True)
                else:
                    print("Orden no reconocida. Usa: kill|start <equipo> <canal> <main|sub>, off|on <equipo>, urls, quit",
                          flush=True)
            except (KeyError, ValueError, TimeoutError) as exc:
                print(f"error: {exc}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
