"""Roles de PostgreSQL por tienda (Row Level Security, migración 0002_site_row_security).

Cada PC de tienda se conecta con su propio rol, que solo puede leer y escribir las filas de su
sede. Se ejecuta en el servidor central con un usuario que pueda crear roles (p. ej. postgres):

    python -m vms.db.site_roles create site-bcn-001 --dsn postgresql://postgres@localhost/vms
    python -m vms.db.site_roles list   --dsn ...
    python -m vms.db.site_roles revoke site-bcn-001 --dsn ...

`create` genera una contraseña aleatoria y la muestra UNA vez, con el VMS_PG_DSN listo para el
.env de la tienda (cámbiale el host si el servidor no se llama igual desde la VPN).
"""
from __future__ import annotations

import argparse
import logging
import secrets
import sys
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from vms.core.naming import is_valid_id
from vms.core.rtsp import redact

log = logging.getLogger(__name__)

# Lo que necesita el proceso de una tienda (analítica + latido directo). Nada de DELETE.
SITE_TABLES = ("sites", "site_cameras", "analytics_rules", "line_counts_minute", "zone_occupancy_minute",
               "queue_alerts", "site_heartbeats")


class SiteRoleError(Exception):
    """Error al crear o revocar el rol de una tienda (mensaje en español para el operador)."""


def role_name_for(site_id: str) -> str:
    """«site-bcn-001» → «vms_site_bcn_001» (identificador PostgreSQL sin comillas)."""
    if not is_valid_id(site_id):
        raise SiteRoleError(f"Identificador de sede no válido: {site_id!r}")
    return "vms_site_" + site_id.removeprefix("site-").replace("-", "_")


def _require_rls(conn: psycopg.Connection[Any]) -> None:
    row = conn.execute("SELECT to_regclass('site_db_roles') IS NOT NULL").fetchone()
    if not row or not row[0]:
        raise SiteRoleError("Falta la migración 0002_site_row_security: ejecuta antes python -m vms.db.migrate")


def create_site_role(conn: psycopg.Connection[Any], site_id: str, password: str | None = None) -> tuple[str, str]:
    """Crea (o rota la contraseña de) el rol de la tienda. Devuelve (rol, contraseña)."""
    _require_rls(conn)
    role = role_name_for(site_id)
    password = password or secrets.token_urlsafe(24)
    ident = sql.Identifier(role)
    with conn.transaction():
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists:
            conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(ident, sql.Literal(password)))
        else:
            conn.execute(sql.SQL("CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                                 "PASSWORD {}").format(ident, sql.Literal(password)))
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in SITE_TABLES), ident))
        conn.execute("INSERT INTO site_db_roles (role_name, site_id) VALUES (%s, %s) "
                     "ON CONFLICT (role_name) DO UPDATE SET site_id = EXCLUDED.site_id", (role, site_id))
    log.info("Rol de tienda %s para %s %s", role, site_id, "actualizado" if exists else "creado")
    return role, password


def revoke_site_role(conn: psycopg.Connection[Any], site_id: str) -> bool:
    """Quita el acceso de la tienda (borra el rol). False si no existía."""
    _require_rls(conn)
    role = role_name_for(site_id)
    ident = sql.Identifier(role)
    with conn.transaction():
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute("DELETE FROM site_db_roles WHERE role_name = %s", (role,))
            return False
        conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in SITE_TABLES), ident))
        conn.execute(sql.SQL("DROP ROLE {}").format(ident))
        conn.execute("DELETE FROM site_db_roles WHERE role_name = %s", (role,))
    return True


def list_site_roles(conn: psycopg.Connection[Any]) -> list[tuple[str, str]]:
    _require_rls(conn)
    return [(r[0], r[1]) for r in conn.execute("SELECT role_name, site_id FROM site_db_roles ORDER BY site_id")]


def site_dsn(admin_dsn: str, role: str, password: str) -> str:
    """DSN para el .env de la tienda: el del servidor con el usuario y la contraseña de la tienda."""
    params = conninfo_to_dict(admin_dsn)
    params.update(user=role, password=password)
    params.setdefault("sslmode", "require")
    host = str(params.get("host") or "")
    if not host or host.startswith("/") or host in ("localhost", "127.0.0.1", "::1"):
        params["host"] = "IP-DEL-SERVIDOR-EN-LA-VPN"
    return make_conninfo("", **{k: v for k, v in params.items() if v not in (None, "")})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vms.db.site_roles",
                                     description="Roles de PostgreSQL por tienda (cada una solo ve sus filas)")
    parser.add_argument("action", choices=["create", "revoke", "list"])
    parser.add_argument("site_id", nargs="?")
    parser.add_argument("--dsn", required=True, help="Conexión de un usuario que pueda crear roles (p. ej. postgres)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if args.action != "list" and not args.site_id:
        print("Indica la sede, p. ej. site-bcn-001", file=sys.stderr)
        return 2
    try:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            if args.action == "list":
                rows = list_site_roles(conn)
                if not rows:
                    print("No hay ningún rol de tienda.")
                for role, site in rows:
                    print(f"{site}\t{role}")
                return 0
            if args.action == "revoke":
                done = revoke_site_role(conn, args.site_id)
                print(f"Rol de {args.site_id} borrado." if done else f"{args.site_id} no tenía rol.")
                return 0 if done else 1
            role, password = create_site_role(conn, args.site_id)
    except SiteRoleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"Error de PostgreSQL: {redact(str(exc))}", file=sys.stderr)
        return 1
    print(f"Rol {role} listo para la sede {args.site_id}. La contraseña no se vuelve a mostrar.\n")
    print("Pon esta línea en el .env de la tienda (revisa el host) y reinicia sus servicios:\n")
    print(f"  VMS_PG_DSN={site_dsn(args.dsn, role, password)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
