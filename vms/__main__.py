"""`python -m vms`: arranca el backend VMS (API, interfaz web y motor de vídeo).

Órdenes:
  python -m vms                    backend (en Windows lo lanza `vmsctl run --service VMSBackend`)
  python -m vms tls-cert ...       certificado autofirmado para HTTPS
  python -m vms engine-config      escribe mediamtx.yml completo para el motor como servicio (modo attach)
  python -m vms engine-run         hace de servicio del motor en desarrollo (lo que en Windows hace vmsctl)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

from vms import APP_NAME, __version__

# Lo fija `vmsctl run`: el proceso para de forma ordenada cuando se cierra su entrada estándar (Windows no
# tiene SIGTERM y un servicio no tiene consola para Ctrl+C/Ctrl+Break).
STOP_ON_STDIN_EOF = "VMS_STOP_ON_STDIN_EOF"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vms", description=f"{APP_NAME}: backend y motor de vídeo")
    parser.add_argument("--version", action="store_true", help="muestra la versión y sale")
    sub = parser.add_subparsers(dest="cmd")
    tls = sub.add_parser("tls-cert", help="crea un certificado autofirmado para servir la web por HTTPS")
    tls.add_argument("--host", action="append", default=[], help="nombre del PC en la red (se puede repetir)")
    tls.add_argument("--ip", action="append", default=[], help="IP del PC en la LAN o la VPN (se puede repetir)")
    tls.add_argument("--days", type=int, default=825, help="días de validez (máximo 825)")
    sub.add_parser("engine-config", help="escribe mediamtx.yml con las rutas de las cámaras (motor como servicio)")
    run = sub.add_parser("engine-run", help="lanza MediaMTX como lo haría el servicio VMSEngine (desarrollo)")
    run.add_argument("--mediamtx", default=None, help="ejecutable de MediaMTX (por defecto VMS_MEDIAMTX_BIN o bin/)")
    args = parser.parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    if args.cmd == "tls-cert":
        return _tls_cert(args.host, args.ip, min(max(args.days, 1), 825))
    if args.cmd == "engine-config":
        return _engine_config()
    if args.cmd == "engine-run":
        return _engine_run(args.mediamtx)

    from pydantic import ValidationError

    from vms.api import create_app
    from vms.core.logging_setup import setup_audit_log, setup_logging
    from vms.core.settings import load_settings

    try:
        settings = load_settings()
    except ValidationError as exc:
        print(f"Configuración (.env) no válida:\n{exc}", file=sys.stderr)
        return 2
    paths = settings.paths.ensure()
    hosted = os.environ.get(STOP_ON_STDIN_EOF) == "1"
    # Bajo vmsctl la consola ya acaba en logs/VMSBackend.log: no duplicar lo que va a vms.log.
    setup_logging(paths.logs_dir, settings.log_level, console=not hosted)
    setup_audit_log(paths.logs_dir)
    log = logging.getLogger("vms")
    log.info("Arrancando %s %s (datos en %s, motor en modo %s)", APP_NAME, __version__, paths.base,
             settings.engine_mode)
    if hosted:
        install_stdin_eof_stop()
    try:
        app = create_app(settings)
    except Exception:  # noqa: BLE001 - se registra y se sale con código de error
        log.exception("No se pudo preparar el backend")
        return 1
    from vms.api.serve import run

    return 0 if run(app, settings) else 3


def install_stdin_eof_stop() -> threading.Thread:
    """Cuando se cierra la entrada estándar (vmsctl pide la parada), simula Ctrl+C: uvicorn para de forma
    ordenada (cierra conexiones, para el latido y el motor) igual que en una consola."""
    def watch() -> None:
        try:
            stdin = sys.stdin.buffer if sys.stdin is not None else None
            while stdin is not None and stdin.read(4096):
                pass
        except (OSError, ValueError):
            pass
        logging.getLogger("vms").info("Parada pedida por el servicio (entrada estándar cerrada)")
        import signal

        try:
            signal.raise_signal(signal.SIGINT)
        except (OSError, ValueError):
            import _thread

            _thread.interrupt_main()

    t = threading.Thread(target=watch, daemon=True, name="stdin-eof-stop")
    t.start()
    return t


def _engine_config() -> int:
    from vms.core.logging_setup import setup_logging
    from vms.core.settings import load_settings
    from vms.engine.service import write_engine_config

    setup_logging(None, "INFO")
    settings = load_settings()
    try:
        file, routes, changed = write_engine_config(settings, settings.paths)
    except Exception as exc:  # noqa: BLE001 - mensaje claro para el instalador (sale con código 1)
        logging.getLogger("vms").exception("No se pudo escribir la configuración del motor")
        print(f"No se pudo escribir la configuración del motor: {exc}", file=sys.stderr)
        return 1
    print(f"{file}: {routes} rutas de cámaras ({'actualizado' if changed else 'sin cambios'})")
    return 0


def _engine_run(mediamtx: str | None) -> int:
    from pathlib import Path

    from vms.core.logging_setup import setup_logging
    from vms.core.paths import find_mediamtx
    from vms.core.settings import load_settings
    from vms.engine.service import install_stop_handlers, run_engine

    settings = load_settings()
    paths = settings.paths.ensure()
    setup_logging(paths.logs_dir, settings.log_level, filename="engine-run.log")
    exe = Path(mediamtx) if mediamtx else (settings.mediamtx_bin or find_mediamtx())
    if exe is None:
        print("No se encuentra MediaMTX: indica --mediamtx o VMS_MEDIAMTX_BIN", file=sys.stderr)
        return 2
    stop = threading.Event()
    install_stop_handlers(stop)
    return run_engine(paths, Path(exe), stop)


def _tls_cert(hosts: list[str], ips: list[str], days: int) -> int:
    from vms.core.settings import load_settings
    from vms.core.tls import create_self_signed

    paths = load_settings().paths.ensure()
    try:
        files = create_self_signed(paths.secrets_dir / "tls", hosts, ips, days=days)
    except ValueError as exc:
        print(f"Dato no válido: {exc}", file=sys.stderr)
        return 2
    print(f"Certificado creado ({days} días):\n  {files.cert}\n  {files.key}\n")
    print("Añade al .env y reinicia el servicio del VMS:\n")
    print(f"  VMS_TLS_CERT_FILE={files.cert}\n  VMS_TLS_KEY_FILE={files.key}\n")
    print("La web quedará en https://<IP>:8643 (VMS_HTTPS_PORT). Abre ese puerto en el firewall y, para que")
    print("los navegadores no avisen, importa vms.crt como raíz de confianza en los PC que la usen (docs/RED.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
