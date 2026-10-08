"""``tools/windows/preparar-entorno.ps1``: el script que deja un PC con Windows listo para desarrollar.

Las funciones se ejecutan de verdad con Windows PowerShell 5.1 (``powershell.exe``), que es el que trae Windows 11
y con el que lo lanza la persona; fuera de Windows esas pruebas se saltan.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.central.test_deploy import _functions_from
from tests.windows.harness import windows_powershell_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "windows" / "preparar-entorno.ps1"


def _windows_powershell() -> str:
    exe = shutil.which("powershell.exe") if sys.platform == "win32" else None
    if not exe:
        pytest.skip("Solo en Windows: hace falta Windows PowerShell 5.1 (powershell.exe)")
    return exe


def _python312_probe(tmp_path: Path, fake_py: str) -> subprocess.CompletedProcess[str]:
    """``Tiene-Python312`` con un ``py`` falso delante en el PATH y ``$ErrorActionPreference = 'Stop'``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "py.cmd").write_text(fake_py, encoding="ascii")
    f = tmp_path / "run.ps1"
    f.write_text(_functions_from(SCRIPT, ["Tiene", "Invoke-Probe", "Tiene-Python312"]) + f"""
$env:Path = '{bin_dir}' + ';' + $env:Path
'RESULTADO=' + [string](Tiene-Python312)
""", encoding="utf-8-sig")
    return subprocess.run([_windows_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(f)], capture_output=True, text=True, timeout=120, env=windows_powershell_env())


def test_python312_probe_survives_py_complaining_on_stderr(tmp_path: Path) -> None:
    """El fallo real: con solo Python 3.13, «py -3.12» avisa por stderr y el script se cortaba en el primer paso."""
    r = _python312_probe(tmp_path, "@echo No suitable Python runtime found 1>&2\r\n@exit /b 103\r\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULTADO=False" in r.stdout


def test_python312_probe_finds_python312(tmp_path: Path) -> None:
    r = _python312_probe(tmp_path, "@echo 1\r\n@exit /b 0\r\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULTADO=True" in r.stdout


def test_chromium_goes_inside_the_venv() -> None:
    """Las pruebas buscan Chromium dentro del venv (``PLAYWRIGHT_BROWSERS_PATH=0`` en ``tests/conftest.py``)."""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "$env:PLAYWRIGHT_BROWSERS_PATH = '0'" in text
    assert text.index("$env:PLAYWRIGHT_BROWSERS_PATH = '0'") < text.index("'playwright', 'install'")


def test_script_is_windows_powershell_5_compatible() -> None:
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "sin BOM, Windows PowerShell 5.1 lo lee como ANSI y rompe las tildes"
    assert "#Requires -Version 5.1" in raw.decode("utf-8-sig")
