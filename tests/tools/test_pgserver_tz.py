"""tools/pgserver_tz.py: copia de pginstall con las zonas horarias de tzdata (la rueda de Windows no las trae)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from tools import pgserver_tz


def _fake_pginstall(base: Path, *, with_tz: bool) -> Path:
    root = base / "pginstall"
    for sub in ("bin", "lib", "share/postgresql/timezonesets"):
        (root / sub).mkdir(parents=True)
    (root / "bin" / "postgres.exe").write_bytes(b"")
    (root / "lib" / "plpgsql.dll").write_bytes(b"")
    if with_tz:
        (root / "share" / "postgresql" / "timezone" / "Europe").mkdir(parents=True)
    return root


def test_copy_with_tzdata_zones_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tzdata")
    src = _fake_pginstall(tmp_path / "venv", with_tz=False)
    monkeypatch.setattr(pgserver_tz, "pginstall_dir", lambda: src)
    bin_dir = pgserver_tz.bin_dir_with_timezones(tmp_path / "tmp")
    assert bin_dir is not None and (bin_dir / "postgres.exe").is_file()
    zones = bin_dir.parent / "share" / "postgresql" / "timezone"
    assert (zones / "Europe" / "Madrid").is_file() and (zones / "Atlantic" / "Canary").is_file()
    assert (zones / "UTC").is_file() and not list(zones.rglob("__init__.py"))
    assert (bin_dir.parent / "share" / "postgresql" / "timezonesets").is_dir()
    assert not src.joinpath("share", "postgresql", "timezone").exists(), "el venv no se toca"
    # Segunda vez: la misma copia, sin volver a copiar
    (bin_dir / "marca").write_text("x", encoding="utf-8")
    assert pgserver_tz.bin_dir_with_timezones(tmp_path / "tmp") == bin_dir
    assert (bin_dir / "marca").is_file()
    assert [p.name for p in (tmp_path / "tmp").iterdir()] == [bin_dir.parent.name], "sin restos a medias"


def test_nothing_to_do_when_pgserver_has_zones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _fake_pginstall(tmp_path / "venv", with_tz=True)
    monkeypatch.setattr(pgserver_tz, "pginstall_dir", lambda: src)
    assert pgserver_tz.bin_dir_with_timezones(tmp_path / "tmp") is None
    assert pgserver_tz.use_pginstall_with_timezones(tmp_path / "tmp") is False
    assert pgserver_tz.use_timezones(tmp_path / "tmp") is True, "sin copia, pero el servidor sí tiene zonas"
    assert not (tmp_path / "tmp").exists()


def test_dev_run_asks_for_utc_whenever_the_server_has_zones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Con una rueda de pgserver que ya trajera las zonas (sin copia), en Windows no se pedía UTC: el servidor se
    quedaba en la zona del PC (Europe/Paris) y fallaba todo lo que espera UTC, como en la CI."""
    from tools.dev_run import EmbeddedPostgres

    src = _fake_pginstall(tmp_path / "venv", with_tz=True)
    monkeypatch.setattr(pgserver_tz, "pginstall_dir", lambda: src)
    asked: list[str] = []

    class Asked(Exception):
        pass

    def force_utc(dsn: str, timeout: float = 10.0) -> None:
        asked.append(dsn)
        raise Asked

    monkeypatch.setattr(pgserver_tz, "force_utc", force_utc)
    fake = types.ModuleType("pgserver")
    uri = "postgresql://postgres@127.0.0.1:1/postgres"
    fake.get_server = lambda pgdata, cleanup_mode: types.SimpleNamespace(get_uri=lambda: uri)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pgserver", fake)
    with pytest.raises(Asked):
        EmbeddedPostgres(tmp_path / "pgdata").start()
    assert asked == [uri]


def test_abandoned_half_copies_are_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Una sesión que muere a mitad de copia deja `<copia>.<pid>` (unos 35 MB) para siempre: la siguiente la borra
    si ese proceso ya no existe. Nunca la de una sesión que sigue copiando."""
    pytest.importorskip("tzdata")
    psutil = pytest.importorskip("psutil")
    src = _fake_pginstall(tmp_path / "venv", with_tz=False)
    monkeypatch.setattr(pgserver_tz, "pginstall_dir", lambda: src)
    done = pgserver_tz.bin_dir_with_timezones(tmp_path / "tmp").parent  # type: ignore[union-attr]
    dead, alive = done.with_name(f"{done.name}.4000000"), done.with_name(f"{done.name}.4000004")
    other = done.with_name(f"{done.name}.antigua")
    for d in (dead, alive, other):
        (d / "bin").mkdir(parents=True)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 4000004)
    assert pgserver_tz.bin_dir_with_timezones(tmp_path / "tmp") == done / "bin"
    assert not dead.exists() and alive.is_dir() and other.is_dir()


@pytest.mark.needs_postgres
def test_stale_server_without_zones_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Transición en Windows: pgserver reutiliza el servidor que siga en marcha con la carpeta de datos, y uno que un
    dev_run anterior arrancó con los binarios de la rueda no conoce «UTC». Antes salía el error crudo de psycopg
    («invalid value for parameter "TimeZone"»), sin decir qué hacer."""
    pgserver = pytest.importorskip("pgserver")
    stock = pgserver_tz.pginstall_dir()
    if pgserver_tz.has_timezones(stock):
        pytest.skip("la rueda de pgserver de este sistema ya trae las zonas horarias (Linux, macOS)")
    import pgserver._commands as pg_commands

    monkeypatch.setattr(pg_commands, "POSTGRES_BIN_PATH", stock / "bin")  # el servidor de antes del arreglo
    srv = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="delete")
    try:
        with pytest.raises(RuntimeError, match="(?s)no tiene zonas horarias.*Qué hacer:.*tools.dev_run stop"):
            pgserver_tz.force_utc(srv.get_uri())
    finally:
        srv.cleanup()


@pytest.mark.needs_postgres
def test_dev_run_embedded_postgres_has_time_zones(tmp_path: Path) -> None:
    """`tools.dev_run start --pg/--central` en Windows: sin zonas, el panel central no abría su grupo de
    conexiones (cada conexión hace SET TIME ZONE 'UTC')."""
    pytest.importorskip("pgserver")
    import psycopg

    from tools.dev_run import EmbeddedPostgres

    pg = EmbeddedPostgres(tmp_path / "pgdata")
    try:
        dsn = pg.start()
        with psycopg.connect(dsn, autocommit=True) as c:
            assert c.execute("SHOW timezone").fetchone() == ("UTC",)
            c.execute("SET TIME ZONE 'Europe/Madrid'")
            assert c.execute("SELECT count(*) FROM pg_timezone_names WHERE name = 'Atlantic/Canary'").fetchone() == (1,)
    finally:
        pg.stop()


def test_missing_tzdata_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    src = _fake_pginstall(tmp_path / "venv", with_tz=False)
    monkeypatch.setattr(pgserver_tz, "pginstall_dir", lambda: src)
    real_import = builtins.__import__

    def no_tzdata(name: str, *args: object, **kwargs: object) -> object:
        if name == "tzdata":
            raise ImportError("sin tzdata")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_tzdata)
    with pytest.raises(pgserver_tz.TimezoneDataMissing, match="tzdata"):
        pgserver_tz.bin_dir_with_timezones(tmp_path / "tmp")
    text = pgserver_tz.NO_TZ_MESSAGE.format(why="falta el paquete tzdata")
    assert "Qué hacer:" in text and "preparar-entorno.ps1" in text
