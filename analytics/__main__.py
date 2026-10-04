"""Punto de entrada: `python -m analytics [run|check]`.

  python -m analytics                  arranca el servicio (configuración desde el backend)
  python -m analytics run --config f.json --seconds 120
                                       configuración fija desde un archivo, durante 120 s
  python -m analytics check            comprueba modelo, backend de inferencia, PostgreSQL y backend
  python -m analytics.reports ...      informe semanal (ver analytics/reports)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from vms.core import aio
from vms.core.logging_setup import setup_logging
from vms.core.settings import load_settings

from .config import load_config_file
from .settings import load_analytics_settings, resolve_backend_url, resolve_models_dir

log = logging.getLogger("analytics")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def handler(signum: int, _frame: object) -> None:
        log.info("Señal %s recibida: parando", signum)
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)  # el servicio de Windows envía Ctrl+Break


async def _run(args: argparse.Namespace) -> int:
    from .service import AnalyticsService, SingleInstanceLock, summarize_rules

    settings = load_settings()
    asettings = load_analytics_settings()
    paths = settings.paths.ensure()
    lock = SingleInstanceLock(paths.analytics_dir / "analytics.lock")
    if not lock.acquire():
        log.error("Ya hay otro proceso de analítica usando %s", paths.analytics_dir)
        return 3
    try:
        static = load_config_file(args.config) if args.config else None
        if static is not None:
            log.info("Configuración fija desde %s: %s", args.config, summarize_rules(static))
        service = AnalyticsService(settings, asettings, static_config=static)
        stop = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop)
        await service.run(stop, max_seconds=args.seconds)
    finally:
        lock.release()
    return 0


async def _check() -> int:
    """Diagnóstico rápido para el instalador. Devuelve 0 si todo lo esencial está bien."""
    import httpx

    from .config import CONFIG_PATH, INTERNAL_TOKEN_HEADER
    from .detector import MODEL_SPECS, DetectorRegistry, DetectorUnavailable, available_backends

    settings = load_settings()
    asettings = load_analytics_settings()
    ok = True
    models_dir = resolve_models_dir(settings, asettings)
    print(f"Carpeta de modelos: {models_dir}")
    print(f"Motores de inferencia instalados: {', '.join(available_backends()) or 'ninguno'}")
    for name in MODEL_SPECS:
        onnx = models_dir / f"{name}.onnx"
        if onnx.is_file():
            try:
                det = DetectorRegistry(models_dir, asettings.inference_backend,
                                       openvino_precision=asettings.openvino_precision).get(name)
                print(f"  {name}: OK ({det.backend})")
            except DetectorUnavailable as exc:
                ok = False
                print(f"  {name}: ERROR {exc}")
    if settings.pg_dsn is None:
        print("PostgreSQL: VMS_PG_DSN no definido (los conteos esperarán en la cola local)")
    else:
        import psycopg

        from vms.core.rtsp import redact
        try:
            async with await psycopg.AsyncConnection.connect(settings.pg_dsn.get_secret_value(),
                                                             connect_timeout=5) as conn:
                await conn.execute("SELECT 1 FROM line_counts_minute LIMIT 1")
            print("PostgreSQL: OK")
        except psycopg.Error as exc:
            ok = False
            print(f"PostgreSQL: ERROR {redact(str(exc))}")
    url = resolve_backend_url(settings, asettings)
    try:
        async with httpx.AsyncClient(base_url=url, timeout=5) as client:
            r = await client.get(CONFIG_PATH, headers={INTERNAL_TOKEN_HEADER: settings.ensure_internal_token()})
        print(f"Backend {url}: HTTP {r.status_code}")
        ok = ok and r.status_code == 200
    except httpx.HTTPError as exc:
        ok = False
        print(f"Backend {url}: sin respuesta ({type(exc).__name__})")
    print(f"Telegram: {'token configurado' if settings.telegram_bot_token else 'sin token'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analytics", description="Analítica de tienda (conteos anónimos)")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="arranca el servicio (por defecto)")
    run.add_argument("--config", type=Path, help="configuración fija (JSON §8.2) en vez de pedirla al backend")
    run.add_argument("--seconds", type=float, help="parar tras N segundos (pruebas)")
    sub.add_parser("check", help="diagnóstico de la instalación")
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["run", *(argv or [])])

    settings = load_settings()
    setup_logging(settings.paths.ensure().logs_dir, settings.log_level, filename="analytics.log")
    if args.cmd == "check":
        return aio.run(_check())
    try:
        return aio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
