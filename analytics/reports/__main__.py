"""CLI del informe semanal.

    python -m analytics.reports --site site-bcn-001 --last-week --out informes/
    python -m analytics.reports --all-sites --week 2026-09-28 --out informes/
    python -m analytics.reports --site site-bcn-001 --last-week --no-llm --no-store

Por defecto: semana ISO anterior, proveedor LLM según .env (VMS_LLM_*), guarda en la tabla
weekly_reports y escribe .md y .html en --out. El servidor central lo programa los lunes a las
06:00 (Europe/Madrid).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import psycopg

from vms.core import aio
from vms.core.rtsp import redact
from vms.core.settings import load_settings

from .generate import generate_weekly_report, list_active_sites, previous_iso_week_start
from .llm import provider_from_settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m analytics.reports", description="Informe semanal por tienda")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--site", action="append", help="id de sede (se puede repetir)")
    who.add_argument("--all-sites", action="store_true", help="todas las sedes activas")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--week", type=date.fromisoformat, help="lunes de la semana (AAAA-MM-DD)")
    when.add_argument("--last-week", action="store_true", help="semana ISO anterior (por defecto)")
    p.add_argument("--out", type=Path, help="carpeta donde escribir .md y .html")
    p.add_argument("--dsn", help="PostgreSQL; por defecto VMS_PG_DSN")
    p.add_argument("--no-llm", action="store_true", help="redactar solo con la plantilla")
    p.add_argument("--no-store", action="store_true", help="no guardar en weekly_reports")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = load_settings()
    dsn = args.dsn or (settings.pg_dsn.get_secret_value() if settings.pg_dsn else None)
    if not dsn:
        print("Falta la conexión a PostgreSQL: usa --dsn o define VMS_PG_DSN", file=sys.stderr)
        return 2
    week = args.week or previous_iso_week_start()
    if week.isoweekday() != 1:
        print(f"{week} no es lunes: indica el lunes de la semana", file=sys.stderr)
        return 2
    provider = None if args.no_llm else provider_from_settings(settings)

    async def run() -> int:
        sites = args.site or await list_active_sites(dsn)
        failures = 0
        for site in sites:
            try:
                rep = await generate_weekly_report(dsn, site, week, provider, store=not args.no_store)
            except LookupError as exc:
                print(f"{site}: {exc}", file=sys.stderr)
                failures += 1
                continue
            if args.out:
                md, ht = rep.write_files(args.out)
                print(f"{site}: {rep.status} ({rep.provider}) → {md} · {ht}")
            else:
                print(rep.markdown)
            if rep.error:
                print(f"{site}: aviso: {rep.error}", file=sys.stderr)
        return 1 if failures else 0

    try:
        return aio.run(run())
    except psycopg.Error as exc:
        print(f"Error de PostgreSQL: {redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
