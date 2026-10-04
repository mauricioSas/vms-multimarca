"""`python -m central` — panel central multi-sede.

Órdenes:
  python -m central [serve]                 arranca el panel (puerto 8700 por defecto)
  python -m central token create <sede>     crea o rota el token de latido de una sede
  python -m central token revoke <sede>     revoca el token de una sede
  python -m central token list              lista las sedes con token (sin mostrar el valor)
  python -m central user add <usuario> [--role admin|operator]   (pide la contraseña)
  python -m central migrate                 aplica las migraciones de PostgreSQL
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys

from vms.core import aio
from vms.core.logging_setup import setup_logging
from vms.core.naming import is_valid_id

from .settings import CentralSettings, load_central_settings

log = logging.getLogger("central")


def _serve(settings: CentralSettings) -> int:
    import uvicorn

    from .app import create_app

    setup_logging(settings.logs_dir, settings.log_level, filename="central.log")
    app = create_app(settings)
    # psycopg asíncrono no funciona con el bucle Proactor que uvicorn elige en Windows
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, log_config=None,
                proxy_headers=False, server_header=False, loop=aio.uvicorn_loop())
    return 0


def _token(settings: CentralSettings, action: str, site_id: str | None) -> int:
    from .security import SiteTokenStore

    settings.ensure_dirs()
    store = SiteTokenStore(settings.tokens_file)
    if action == "list":
        rows = store.list()
        if not rows:
            print("No hay ninguna sede con token.")
        for r in rows:
            print(f"{r['site_id']}\tcreado {r['created_at']}")
        return 0
    if not site_id or not is_valid_id(site_id):
        print("Indica un identificador de sede válido (minúsculas, números y guiones; 3-40)", file=sys.stderr)
        return 2
    if action == "create":
        token = store.issue(site_id)
        print(f"Token de la sede {site_id} (cópialo ahora, no se vuelve a mostrar):\n\n  {token}\n")
        print("Ponlo en el .env de la sede como VMS_SITE_TOKEN=... y reinicia el servicio de latido.")
        return 0
    if store.revoke(site_id):
        print(f"Token de {site_id} revocado.")
        return 0
    print(f"La sede {site_id} no tenía token.", file=sys.stderr)
    return 1


def _user_add(settings: CentralSettings, username: str, role: str) -> int:
    from pydantic import SecretStr, ValidationError

    from vms.core.config_store import UserStore
    from vms.core.models import User, UserCreate

    from .security import hash_password

    settings.ensure_dirs()
    store = UserStore(settings.users_file)
    if store.get(username):
        print("Ese usuario ya existe.", file=sys.stderr)
        return 1
    pw = getpass.getpass("Contraseña (mínimo 8 caracteres): ")
    if pw != getpass.getpass("Repite la contraseña: "):
        print("Las contraseñas no coinciden.", file=sys.stderr)
        return 1
    try:
        data = UserCreate(username=username, password=SecretStr(pw), role=role)  # type: ignore[arg-type]
    except ValidationError as exc:
        for e in exc.errors():
            print(e["msg"], file=sys.stderr)
        return 1
    aio.run(store.save_user(User(username=data.username, role=data.role,
                                     password_hash=hash_password(pw))))
    print(f"Usuario {data.username} ({data.role}) creado.")
    return 0


def _migrate(settings: CentralSettings) -> int:
    from vms.db.migrate import main as migrate_main

    if not settings.pg_dsn:
        print("Falta VMS_CENTRAL_PG_DSN (o VMS_PG_DSN)", file=sys.stderr)
        return 2
    return migrate_main(["--dsn", settings.pg_dsn.get_secret_value()])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m central", description="Panel central multi-sede")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Arranca el panel")
    t = sub.add_parser("token", help="Tokens de latido por sede")
    t.add_argument("action", choices=["create", "revoke", "list"])
    t.add_argument("site_id", nargs="?")
    u = sub.add_parser("user", help="Usuarios del panel")
    u.add_argument("action", choices=["add"])
    u.add_argument("username")
    u.add_argument("--role", choices=["admin", "operator"], default="operator")
    sub.add_parser("migrate", help="Aplica las migraciones de PostgreSQL")
    args = parser.parse_args(argv)
    settings = load_central_settings()
    if args.cmd in (None, "serve"):
        return _serve(settings)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if args.cmd == "token":
        return _token(settings, args.action, args.site_id)
    if args.cmd == "user":
        return _user_add(settings, args.username, args.role)
    return _migrate(settings)


if __name__ == "__main__":
    raise SystemExit(main())
