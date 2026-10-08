"""PostgreSQL embebido (pgserver) con zonas horarias también en Windows. Solo pruebas y modo desarrollo.

La rueda de Windows de pgserver 0.1.4 (la última publicada) no trae `share/postgresql/timezone`, y
PostgreSQL 16 solo busca las zonas en `<carpeta de postgres.exe>/../share/postgresql/timezone` (se fija al
compilar: no hay variable ni ajuste para cambiarla). Sin ellas fallan `SET TIME ZONE 'UTC'`,
`AT TIME ZONE 'Europe/Madrid'` y `pg_timezone_names`, que usan el panel central, la analítica y los
informes; el grupo de conexiones del panel central descarta todas las conexiones y acaba en `PoolTimeout`.
La rueda de Linux sí las trae.

Arreglo: una copia de `pginstall` en `<repo>/.tmp` con las zonas del paquete `tzdata` (en Windows ya lo
instalan los locks del producto) y pgserver apuntando a esa copia. El producto no usa nada de esto: en una
tienda o en el panel central se usa un PostgreSQL normal.
"""
from __future__ import annotations

import os
import shutil
import time
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TMP = ROOT / ".tmp"
DONE_MARK = ".completa"

NO_TZ_MESSAGE = ("El PostgreSQL embebido (pgserver) no tiene datos de zonas horarias ({why}): fallará todo lo que "
                 "las usa (panel central, analítica, informes y sus pruebas de central/, analytics/ y db/). Qué hacer: "
                 "ejecuta tools\\windows\\preparar-entorno.ps1 para instalar tzdata en el venv, o define "
                 "VMS_TEST_PG_DSN con un PostgreSQL que tenga zonas horarias.")


class TimezoneDataMissing(RuntimeError):
    """No se pudo preparar la copia de pginstall con zonas horarias (falta tzdata o falló la copia)."""


def pginstall_dir() -> Path:
    import pgserver

    return Path(pgserver.__file__).parent / "pginstall"


def has_timezones(pginstall: Path) -> bool:
    return (pginstall / "share" / "postgresql" / "timezone").is_dir()


def bin_dir_with_timezones(tmp_root: Path = DEFAULT_TMP) -> Path | None:
    """Carpeta `bin` de un pginstall con zonas horarias; None si el de pgserver ya las trae (Linux, macOS).

    La copia se prepara una vez por versión de pgserver y de tzdata (unos 35 MB, menos de un segundo) y se
    publica con un rename atómico: varias sesiones de pytest pueden arrancar a la vez. Nunca se borra (un
    servidor que siga vivo la tendría abierta); una versión nueva va a otra carpeta. Lo que sí se borra son las
    copias a medias de una sesión que murió copiando.
    """
    src = pginstall_dir()
    if has_timezones(src):
        return None
    try:
        import tzdata
    except ImportError as exc:
        raise TimezoneDataMissing("falta el paquete tzdata") from exc
    dst = tmp_root / f"pginstall-{version('pgserver')}-tz{tzdata.IANA_VERSION}"
    _remove_abandoned_copies(dst)
    if not (dst / DONE_MARK).is_file():
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            for sub in ("bin", "lib", "share"):
                shutil.copytree(src / sub, tmp / sub)
            shutil.copytree(Path(tzdata.__file__).parent / "zoneinfo", tmp / "share" / "postgresql" / "timezone",
                            ignore=shutil.ignore_patterns("__init__.py", "__pycache__", "*.pyc"))
            (tmp / DONE_MARK).write_text("ok", encoding="utf-8")
            tmp.rename(dst)
        except OSError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            if not (dst / DONE_MARK).is_file():  # si otra sesión la publicó a la vez, vale la suya
                raise TimezoneDataMissing(f"no se pudo copiar pginstall en {dst}: {exc}") from exc
    return dst / "bin"


def _remove_abandoned_copies(dst: Path) -> None:
    """Borra las copias a medias (`<dst>.<pid>`, unos 35 MB cada una) de sesiones que murieron copiando: la de cada
    sesión solo la borra ella misma. Nunca la de un proceso vivo; sin psutil no se toca nada."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil va en los locks del producto y de pruebas
        return
    for leftover in dst.parent.glob(f"{dst.name}.*"):
        pid = leftover.name[len(dst.name) + 1:]
        if pid.isdigit() and int(pid) != os.getpid() and not psutil.pid_exists(int(pid)):
            shutil.rmtree(leftover, ignore_errors=True)


def use_pginstall_with_timezones(tmp_root: Path = DEFAULT_TMP) -> bool:
    """Hace que pgserver lance initdb, pg_ctl y postgres desde la copia con zonas. True si hizo falta.

    Cambia un atributo privado de pgserver (fijado en 0.1.4 por los locks de pruebas): sus órdenes leen
    `POSTGRES_BIN_PATH` en cada llamada. Lanza TimezoneDataMissing si no se pudo preparar la copia.
    """
    bin_dir = bin_dir_with_timezones(tmp_root)
    if bin_dir is None:
        return False
    import pgserver._commands as pg_commands

    pg_commands.POSTGRES_BIN_PATH = bin_dir
    return True


def use_timezones(tmp_root: Path = DEFAULT_TMP) -> bool:
    """Prepara pgserver con zonas horarias. True si el servidor las tendrá, porque las trae la rueda (Linux, macOS
    o una versión futura en Windows) o porque se usa la copia: solo entonces se puede pedir UTC (`force_utc`).
    Lanza TimezoneDataMissing si hacía falta la copia y no se pudo preparar."""
    return use_pginstall_with_timezones(tmp_root) or has_timezones(pginstall_dir())


def force_utc(dsn: str, timeout: float = 10.0) -> None:
    """Zona del servidor en UTC, como en Linux (CI) y como el GMT de antes en Windows.

    Con las zonas instaladas, initdb toma la de Windows (Europe/Paris en un PC en España), que cambia de un PC
    a otro. Se espera a que las conexiones nuevas ya la vean: `pg_reload_conf()` solo envía la señal.
    """
    import psycopg

    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("ALTER SYSTEM SET timezone = 'UTC'")
            conn.execute("ALTER SYSTEM SET log_timezone = 'UTC'")
            conn.execute("SELECT pg_reload_conf()")
    except psycopg.errors.InvalidParameterValue as exc:
        # pgserver reutiliza el servidor que siga en marcha con esa carpeta de datos: uno arrancado antes con los
        # binarios de la rueda (un dev_run sin parar de antes de este arreglo) no conoce ni «UTC».
        raise RuntimeError(f"El PostgreSQL embebido que ya estaba en marcha no tiene zonas horarias ({exc}). Qué "
                           "hacer: páralo («python -m tools.dev_run stop») y vuelve a arrancar; así se lanza con "
                           "las zonas de tools/pgserver_tz.py.") from exc
    deadline = time.monotonic() + timeout
    while True:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute("SHOW timezone").fetchone()
        if row and row[0] == "UTC":
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"PostgreSQL sigue en la zona {row[0] if row else '?'} {timeout:.0f} s después de "
                               "pedir UTC. Qué hacer: vuelve a arrancar; si se repite, para el PostgreSQL embebido "
                               "(«python -m tools.dev_run stop» o cierra la sesión de pytest) y vuelve a empezar.")
        time.sleep(0.05)
