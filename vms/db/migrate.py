"""Aplica las migraciones SQL de vms/db/migrations en orden, una sola vez cada una.

Uso:  python -m vms.db.migrate --dsn postgresql://usuario@host/base
      (o con VMS_PG_DSN en el entorno / .env)

Cada archivo NNNN_nombre.sql se aplica en su propia transacción y se anota en
schema_migrations. Un bloqueo consultivo evita que dos procesos migren a la vez.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import psycopg

from vms.core.rtsp import redact

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 815_204_771  # constante arbitraria para pg_advisory_lock


def available_migrations() -> list[tuple[str, Path]]:
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    return [(f.stem, f) for f in files]


def apply_migrations(dsn: str) -> list[str]:
    """Devuelve la lista de versiones aplicadas en esta llamada."""
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                version text PRIMARY KEY,
                                applied_at timestamptz NOT NULL DEFAULT now())""")
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for version, path in available_migrations():
                if version in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                with conn.transaction():
                    conn.execute(sql)  # type: ignore[arg-type]  # SQL de archivo propio, sin parámetros
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                log.info("Migración aplicada: %s", version)
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


def main(argv: list[str] | None = None) -> int:
    from vms.core.settings import load_settings

    parser = argparse.ArgumentParser(description="Aplica las migraciones de PostgreSQL")
    parser.add_argument("--dsn", help="Cadena de conexión; por defecto VMS_PG_DSN")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dsn = args.dsn or (load_settings().pg_dsn.get_secret_value() if load_settings().pg_dsn else None)
    if not dsn:
        print("Falta la cadena de conexión: usa --dsn o define VMS_PG_DSN", file=sys.stderr)
        return 2
    try:
        done = apply_migrations(dsn)
    except psycopg.Error as exc:
        print(f"Error al migrar: {redact(str(exc))}", file=sys.stderr)
        return 1
    print("Sin migraciones pendientes" if not done else f"Aplicadas: {', '.join(done)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
