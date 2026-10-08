"""Qué .env lee `load_settings()` (CONTRATO §3.1) y que las pruebas no lean nunca el del producto instalado.

Regresión del PC con Windows y el producto instalado: `tests/analytics/test_reports.py::
test_cli_writes_markdown_and_html` llamaba a `load_settings()` sin VMS_DATA_DIR, `env_files()` añadía siempre
`%PROGRAMDATA%\\VMSMultimarca\\.env` y la prueba fallaba con «Permission denied» (como administrador habría leído
la configuración real). Con VMS_ENV_FILE_ONLY=1 (lo fija tests/conftest.py) solo se lee VMS_ENV_FILE.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import vms.core.settings as st
from tests.conftest import REAL_INSTALL_GUARD, ROOT, RealInstallTouched

MARKER = "VMS_SITE_ID=site-real-instalado\nVMS_HTTP_PORT=9999\nVMS_PG_DSN=postgresql://marcador@instalado/db\n"


def _fake_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Carpeta de datos «del sistema» y carpeta de instalación falsas, cada una con su .env marcador."""
    data = tmp_path / "ProgramData" / "VMSMultimarca"
    inst = tmp_path / "Program Files" / "VMSMultimarca"
    for d in (data, inst):
        d.mkdir(parents=True)
        (d / ".env").write_text(MARKER, encoding="utf-8")
    monkeypatch.delenv("VMS_DATA_DIR", raising=False)
    monkeypatch.setattr(st, "default_data_dir", lambda: data)
    monkeypatch.setattr(st, "install_dir", lambda: inst)
    return data, inst


def test_conftest_isolates_env_files() -> None:
    assert os.environ["VMS_ENV_FILE_ONLY"] == "1" and st.env_files_only()


def test_tests_never_read_install_or_system_env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(tmp_path, monkeypatch)
    assert st.env_files() == []
    s = st.load_settings()
    assert (s.site_id, s.http_port, s.pg_dsn) == ("site-local", 8600, None)


def test_explicit_env_file_is_still_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(tmp_path, monkeypatch)
    explicit = tmp_path / "pruebas.env"
    explicit.write_text("VMS_SITE_ID=site-pruebas\n", encoding="utf-8")
    monkeypatch.setenv("VMS_ENV_FILE", str(explicit))
    assert st.env_files() == [explicit]
    assert st.load_settings().site_id == "site-pruebas"


@pytest.mark.skipif(sys.platform != "win32", reason="ruta por defecto de Windows (%PROGRAMDATA%)")
def test_programdata_env_not_read_under_pytest_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Las mismas condiciones que test_cli_writes_markdown_and_html, con un ProgramData falso."""
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.delenv("VMS_DATA_DIR", raising=False)
    (tmp_path / "VMSMultimarca").mkdir()
    (tmp_path / "VMSMultimarca" / ".env").write_text(MARKER, encoding="utf-8")
    assert st.default_data_dir() == tmp_path / "VMSMultimarca"
    assert st.load_settings().site_id == "site-local"


@pytest.mark.parametrize("value", ["", "0", "no"])
def test_production_search_order_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Sin VMS_ENV_FILE_ONLY (como en una instalación): <instalación> → <datos> → VMS_ENV_FILE."""
    data, inst = _fake_system(tmp_path, monkeypatch)
    monkeypatch.setenv("VMS_ENV_FILE_ONLY", value)
    (inst / ".env").write_text("VMS_SITE_ID=site-instalacion\nVMS_HTTP_PORT=9001\n", encoding="utf-8")
    (data / ".env").write_text("VMS_HTTP_PORT=9002\n", encoding="utf-8")
    explicit = tmp_path / "x.env"
    explicit.write_text("VMS_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("VMS_ENV_FILE", str(explicit))
    assert st.env_files() == [inst / ".env", data / ".env", explicit]
    s = st.load_settings()
    assert (s.site_id, s.http_port, s.log_level) == ("site-instalacion", 9002, "DEBUG")


def test_child_processes_of_dev_run_do_not_read_the_system_env(tmp_path: Path) -> None:
    """tools/dev_run.base_env quita las VMS_* y deja VMS_DATA_DIR solo en el .env de desarrollo: el proceso hijo
    resolvía la carpeta de datos del sistema y leía su .env (en este PC, PermissionError al arrancar)."""
    from tools.dev_run import base_env, write_env_file

    fake = tmp_path / "sistema"
    for d in (fake / "VMSMultimarca", fake / "vms-multimarca"):  # Windows (PROGRAMDATA) y Linux (XDG_DATA_HOME)
        d.mkdir(parents=True)
        (d / ".env").write_text(MARKER, encoding="utf-8")
    env_file = tmp_path / "dev.env"
    write_env_file(env_file, {"VMS_DATA_DIR": str(tmp_path / "datos"), "VMS_HTTP_PORT": "8611"})
    env = base_env(env_file, {"PROGRAMDATA": str(fake), "XDG_DATA_HOME": str(fake)})
    env.pop("VMS_DATA_DIR", None)  # solo VMS_ENV_FILE_ONLY: VMS_DATA_DIR únicamente dentro del .env, como antes
    code = ("from vms.core.settings import env_files, load_settings; s = load_settings(); "
            "print([str(p) for p in env_files()]); print(s.site_id, s.http_port, s.data_dir)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert "site-real-instalado" not in out.stdout and "9999" not in out.stdout, out.stdout
    assert f"site-local 8611 {tmp_path / 'datos'}" in out.stdout, out.stdout


def test_dev_run_exports_the_data_dir_as_the_settings_read_it(tmp_path: Path) -> None:
    """La variable VMS_DATA_DIR gana al .env: tiene que llevar el valor ya interpretado (sin comillas ni comentario
    al final, como python-dotenv). Antes se copiaba el texto tal cual y el backend usaba la carpeta «'...'»."""
    from tools.dev_run import base_env

    data = tmp_path / "ruta con espacios"
    env_file = tmp_path / "mio.env"
    env_file.write_text(f"VMS_DATA_DIR='{data}'  # carpeta de datos\nVMS_HTTP_PORT=8611\n", encoding="utf-8")
    assert base_env(env_file)["VMS_DATA_DIR"] == str(data)
    s = st.load_settings(env_file)
    assert s.data_dir == data, "lo mismo que leen los ajustes del propio .env"


def test_dev_run_exports_the_data_dir_and_refuses_an_env_file_without_it(tmp_path: Path) -> None:
    from tools.dev_run import DevOptions, DevStack, base_env, write_env_file

    env_file = tmp_path / "mio.env"
    write_env_file(env_file, {"VMS_HTTP_PORT": "8611"})
    with pytest.raises(RuntimeError, match="no define VMS_DATA_DIR.*Qué hacer:"):
        DevStack(DevOptions(workdir=tmp_path / "dev", env_file=env_file)).prepare_env()
    write_env_file(env_file, {"VMS_HTTP_PORT": "8611", "VMS_DATA_DIR": str(tmp_path / "datos")})
    assert DevStack(DevOptions(workdir=tmp_path / "dev", env_file=env_file)).prepare_env()["VMS_HTTP_PORT"] == "8611"
    env = base_env(env_file)
    assert env["VMS_ENV_FILE_ONLY"] == "1" and env["VMS_DATA_DIR"] == str(tmp_path / "datos")


def test_real_install_guard_blocks_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El vigilante de tests/conftest.py: abrir, crear o listar dentro de la instalación real falla (y se apunta)."""
    real = tmp_path / "ProgramData" / "VMSMultimarca"
    real.mkdir(parents=True)
    (real / ".env").write_text(MARKER, encoding="utf-8")
    sibling = tmp_path / "ProgramData" / "VMSMultimarca-pruebas"  # otra carpeta, aunque empiece igual
    monkeypatch.setattr(REAL_INSTALL_GUARD, "roots", (os.path.normcase(str(real)),))
    try:
        with pytest.raises(RealInstallTouched):
            (real / ".env").read_text(encoding="utf-8")
        with pytest.raises(RealInstallTouched):
            (real / "config").mkdir()
        with pytest.raises(RealInstallTouched):
            os.listdir(real)
        sibling.mkdir()
        (sibling / "x.txt").write_text("ok", encoding="utf-8")
        assert len(REAL_INSTALL_GUARD.hits) == 3
    finally:
        REAL_INSTALL_GUARD.hits.clear()  # si no, el propio vigilante haría fallar esta prueba al terminar


def test_real_install_guard_blocks_running_programs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lanzar un programa de la instalación real, o pasarle una ruta de dentro, también falla.

    Regresión: en Windows, subprocess.Popen avisa con la línea de órdenes ya montada en un solo texto
    (list2cmdline). Con espacios en la ruta, como en «C:\\Program Files», el programa llega entre comillas
    («"C:\\Program Files\\VMSMultimarca\\...\\vmsctl.exe" health») y el vigilante lo dejaba pasar: el
    vmsctl.exe instalado se habría ejecutado de verdad.
    """
    prog = tmp_path / "Program Files" / "VMSMultimarca"
    data = tmp_path / "ProgramData" / "VMSMultimarca"
    sibling = tmp_path / "ProgramData" / "VMSMultimarca-pruebas"  # otra carpeta, aunque empiece igual
    for d in (prog, data, sibling):
        d.mkdir(parents=True)
    exe = prog / "versions" / "2.0.0" / "bin" / "vmsctl.exe"
    monkeypatch.setattr(REAL_INSTALL_GUARD, "roots", tuple(os.path.normcase(str(d)) for d in (prog, data)))
    try:
        with pytest.raises(RealInstallTouched):
            subprocess.run([str(exe), "health"], capture_output=True, check=False)
        with pytest.raises(RealInstallTouched):  # una ruta de dentro como argumento (powershell -File …)
            subprocess.run([sys.executable, "-c", "pass", str(data / "x.ps1")], capture_output=True, check=False)
        with pytest.raises(RealInstallTouched):
            subprocess.run([sys.executable, "-c", "pass"], cwd=data, capture_output=True, check=False)
        with pytest.raises(RealInstallTouched):
            os.system(f'"{exe}" health')
        subprocess.run([sys.executable, "-c", "pass", str(sibling / "x.ps1")], capture_output=True, check=True)
        assert len(REAL_INSTALL_GUARD.hits) == 4
        assert all("vmsmultimarca-pruebas" not in hit.lower() for hit in REAL_INSTALL_GUARD.hits)
    finally:
        REAL_INSTALL_GUARD.hits.clear()


@pytest.mark.skipif(sys.platform != "win32", reason="carpetas del producto instalado en Windows")
def test_real_install_guard_watches_the_folders_in_the_registry(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Una instalación en otra carpeta (InstallDir y DataDir de HKLM\\SOFTWARE\\VMSMultimarca) también se vigila;
    una unidad entera o una carpeta que contenga el repositorio, no (pararía todas las pruebas)."""
    import tests.conftest as conftest

    custom = tmp_path / "D" / "Videovigilancia"
    monkeypatch.setattr(conftest, "_registered_install_dirs", lambda: [str(custom), "C:\\", str(ROOT.parent)])
    roots = conftest._real_install_dirs()
    assert os.path.normcase(str(custom)) in roots
    assert os.path.normcase(os.path.join(os.environ["ProgramData"], "VMSMultimarca")) in roots
    assert os.path.normcase("C:\\") not in roots and os.path.normcase(str(ROOT.parent)) not in roots
