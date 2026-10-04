"""`python -m vms`: arranca el backend VMS (API, interfaz web y motor de vídeo)."""
from __future__ import annotations

import argparse
import logging
import sys

from vms import APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vms", description=f"{APP_NAME}: backend y motor de vídeo")
    parser.add_argument("--version", action="store_true", help="muestra la versión y sale")
    sub = parser.add_subparsers(dest="cmd")
    tls = sub.add_parser("tls-cert", help="crea un certificado autofirmado para servir la web por HTTPS")
    tls.add_argument("--host", action="append", default=[], help="nombre del PC en la red (se puede repetir)")
    tls.add_argument("--ip", action="append", default=[], help="IP del PC en la LAN o la VPN (se puede repetir)")
    tls.add_argument("--days", type=int, default=825, help="días de validez (máximo 825)")
    args = parser.parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    if args.cmd == "tls-cert":
        return _tls_cert(args.host, args.ip, min(max(args.days, 1), 825))

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
    setup_logging(paths.logs_dir, settings.log_level)
    setup_audit_log(paths.logs_dir)
    log = logging.getLogger("vms")
    log.info("Arrancando %s %s (datos en %s)", APP_NAME, __version__, paths.base)
    try:
        app = create_app(settings)
    except Exception:  # noqa: BLE001 - se registra y se sale con código de error
        log.exception("No se pudo preparar el backend")
        return 1
    from vms.api.serve import run

    return 0 if run(app, settings) else 3


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
