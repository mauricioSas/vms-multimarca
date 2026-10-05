"""Runtime de Windows (`python -m distribution.runtime.build`, PLAN-V2 §1.2), sin red: Python embebible de
mentira y una wheel mínima construida en la prueba. La construcción real (python.org + PyPI para win_amd64)
la hace el job de Windows de B1."""
from __future__ import annotations

import hashlib
import marshal
import sys
import zipfile
from pathlib import Path

import pytest

from distribution.runtime import build as rb

ROOT = Path(__file__).resolve().parents[2]


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_wheel(folder: Path, name: str = "vmsprueba", version: str = "1.0") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    whl = folder / f"{name}-{version}-py3-none-any.whl"
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "VALORES = frozenset({'a', 'b', 'c', 'd'})\n\ndef hola() -> str:\n    return 'hola'\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: prueba\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{k},,\n" for k in files) + f"{dist}/RECORD,,\n"
    with zipfile.ZipFile(whl, "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
        zf.writestr(f"{dist}/RECORD", record)
    return whl


def make_embed_zip(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    z = folder / "python-embed-de-prueba.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("python.exe", b"MZ-de-mentira")
        zf.writestr("python312.dll", b"MZ-de-mentira")
        zf.writestr("python312.zip", b"PK\x05\x06" + b"\x00" * 18)
        zf.writestr("python312._pth", "python312.zip\n.\n#import site\n")
    return z


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    whl = make_wheel(tmp_path / "wheels")
    req = tmp_path / "requirements-prueba.txt"
    req.write_text(
        "# lock de prueba\n"
        f"vmsprueba==1.0 \\\n    --hash=sha256:{sha256(whl)}\n"
        f"uvloop==0.23.0 ; sys_platform == \"darwin\" or sys_platform == \"linux\" \\\n    --hash=sha256:{'0' * 64}\n",
        encoding="utf-8")
    return make_embed_zip(tmp_path / "py"), req, tmp_path / "wheels"


def run_build(tmp_path: Path, inputs: tuple[Path, Path, Path], out_name: str) -> Path:
    zip_path, req, wheels = inputs
    return rb.build(tmp_path / out_name, [req], cache=tmp_path / "cache", python_zip=zip_path,
                    python_sha256=sha256(zip_path), find_links=[str(wheels)], no_index=True)


def test_build_layout_pth_pyc_and_manifest(tmp_path: Path, inputs: tuple[Path, Path, Path]) -> None:
    out = run_build(tmp_path, inputs, "runtime")
    pth = (out / "python312._pth").read_bytes()
    assert pth == b"python312.zip\r\n.\r\nLib\\site-packages\r\n..\\app\r\nimport site\r\n"
    site = out / "Lib" / "site-packages"
    assert (site / "vmsprueba" / "__init__.py").is_file()
    assert not (site / "uvloop").exists(), "los marcadores se evalúan para Windows"
    assert not (site / "bin").exists()
    pyc = site / "vmsprueba" / "__pycache__" / "__init__.cpython-312.pyc"
    data = pyc.read_bytes()
    assert int.from_bytes(data[4:8], "little") == 0b01, "pyc basado en hash y sin comprobar (unchecked-hash)"
    code = marshal.loads(data[16:])
    # Ruta relativa (compileall une con el separador del sistema que construye)
    assert code.co_filename.replace("\\", "/") == "Lib/site-packages/vmsprueba/__init__.py"
    manifest = (out / rb.MANIFEST).read_text(encoding="utf-8").splitlines()
    rels = [line.split("  ", 1)[1] for line in manifest]
    assert rels == sorted(rels) and "python.exe" in rels and "Lib/site-packages/vmsprueba/__init__.py" in rels
    assert rb.verify_manifest(out) == []
    (site / "vmsprueba" / "__init__.py").write_text("manipulado\n", encoding="utf-8")
    (out / "extra.txt").write_text("x", encoding="utf-8")
    assert sorted(rb.verify_manifest(out)) == ["modificado Lib/site-packages/vmsprueba/__init__.py", "sobra extra.txt"]


def test_two_builds_give_the_same_manifest(tmp_path: Path, inputs: tuple[Path, Path, Path]) -> None:
    a = run_build(tmp_path, inputs, "a")
    b = run_build(tmp_path, inputs, "b")
    assert (a / rb.MANIFEST).read_bytes() == (b / rb.MANIFEST).read_bytes()


def test_wrong_python_hash_is_refused(tmp_path: Path, inputs: tuple[Path, Path, Path]) -> None:
    zip_path, req, wheels = inputs
    with pytest.raises(rb.BuildError, match="SHA-256 incorrecto"):
        rb.build(tmp_path / "x", [req], cache=tmp_path / "c", python_zip=zip_path, python_sha256="0" * 64)


def test_tampered_wheel_is_refused_by_pip_hashes(tmp_path: Path, inputs: tuple[Path, Path, Path]) -> None:
    zip_path, req, wheels = inputs
    req.write_text(f"vmsprueba==1.0 --hash=sha256:{'1' * 64}\n", encoding="utf-8")
    with pytest.raises(rb.BuildError, match="pip install falló"):
        run_build(tmp_path, (zip_path, req, wheels), "y")


def test_markers_are_evaluated_for_windows(tmp_path: Path) -> None:
    h = "--hash=sha256:" + "a" * 64
    req = tmp_path / "r.txt"
    req.write_text(f'colorama==0.4.6 ; sys_platform == "win32" {h}\n'
                   f'jeepney==0.9.0 ; sys_platform == "linux" {h}\n'
                   f"Pydantic_Core==2.1 {h}\n", encoding="utf-8")
    assert [p.name for p in rb.windows_pins([req])] == ["colorama", "pydantic-core"]
    other = tmp_path / "o.txt"
    other.write_text(f"pydantic-core==2.2 {h}\n", encoding="utf-8")
    with pytest.raises(rb.BuildError, match="versiones distintas"):
        rb.windows_pins([req, other])
    nohash = tmp_path / "n.txt"
    nohash.write_text("requests==2.0\n", encoding="utf-8")
    with pytest.raises(rb.BuildError, match="sin --hash"):
        rb.windows_pins([nohash])


def test_real_site_locks_resolve_for_windows() -> None:
    pins = {p.name: p for p in rb.windows_pins([ROOT / f for f in rb.DEFAULT_REQUIREMENTS])}
    assert {"fastapi", "uvicorn", "pydantic", "cryptography", "opencv-python-headless"} <= set(pins)
    assert "colorama" in pins and "tzdata" in pins and "pywin32-ctypes" in pins, "dependencias solo de Windows"
    assert not {"uvloop", "jeepney", "secretstorage"} & set(pins), "dependencias de Linux/macOS fuera"
    assert all("--hash=sha256:" in p.line and ";" not in p.line for p in pins.values())


def test_cli_verify(tmp_path: Path, inputs: tuple[Path, Path, Path]) -> None:
    out = run_build(tmp_path, inputs, "rt")
    assert rb.main(["--out", str(out), "--verify"]) == 0
    (out / "python.exe").write_bytes(b"otro")
    assert rb.main(["--out", str(out), "--verify"]) == 1


@pytest.mark.skipif(sys.version_info[:2] == (3, 12), reason="solo comprueba la protección con otro Python")
def test_refuses_other_python_versions(tmp_path: Path) -> None:  # pragma: no cover
    with pytest.raises(rb.BuildError, match="3.12"):
        rb.build(tmp_path / "x", [], cache=tmp_path)
