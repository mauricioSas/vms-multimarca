"""Payload por versión y zips reproducibles (PLAN-V2 §2.4, §2.7 y §4.1; ``distribution/layout.py``)."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from distribution.layout import (
    REPRODUCIBLE,
    LayoutError,
    LayoutInputs,
    build_layout,
    compare_builds,
    numeric_version,
    parse_version,
    sha256_file,
)

EPOCH = 1_759_622_400   # 2025-10-05


@pytest.fixture
def artifacts(tmp_path: Path) -> dict[str, Path]:
    bins = tmp_path / "bins"
    bins.mkdir()
    for name in ("vmshost.exe", "vmsctl.exe", "VMS.exe"):
        (bins / name).write_bytes(b"MZ" + name.encode() * 20)
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "mediamtx.exe").write_bytes(b"MZ-mediamtx")
    (engine / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"MZ-python")
    (runtime / "python312._pth").write_text("python312.zip\n.\nLib\\site-packages\n..\\app\n", encoding="utf-8")
    (runtime / "Lib" / "site-packages" / "distutils-precedence.pth").write_text("x\n", encoding="utf-8")
    (runtime / "VERSION").write_text("3.12.10-r1\n", encoding="utf-8")
    return {"bins": bins, "engine": engine, "runtime": runtime}


def _inputs(a: dict[str, Path], **kw: object) -> LayoutInputs:
    base: dict[str, object] = dict(version="2.0.0-ci.7", vmshost=a["bins"] / "vmshost.exe",
                                   vmsctl=a["bins"] / "vmsctl.exe", viewer=a["bins"] / "VMS.exe",
                                   engine_dir=a["engine"], runtime_dir=a["runtime"], epoch=EPOCH)
    base.update(kw)
    return LayoutInputs(**base)  # type: ignore[arg-type]


def test_versions() -> None:
    assert parse_version("2.0.0-dev.5") == (2, 0, 0, "dev.5")
    assert numeric_version("2.1.3-ci.9") == "2.1.3.0"
    for bad in ("2.0", "v2.0.0", "2.0.0.0", "02.0.0", "2.0.0-"):
        with pytest.raises(LayoutError):
            parse_version(bad)


def test_layout_matches_contract_13_1(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    res = build_layout(_inputs(artifacts), tmp_path / "out")
    p = res.payload
    v = p / "versions" / "2.0.0-ci.7"
    for rel in ("bin/vmshost.exe", "updater/slot-a/vmsctl.exe"):
        assert (p / rel).is_file(), rel
    for rel in ("bin/vmsctl.exe", "runtime/python.exe", "runtime/python312._pth", "app/vms/__init__.py",
                "app/analytics/__init__.py", "app/central/__init__.py", "engine/mediamtx.exe",
                "engine/MEDIAMTX-LICENSE.txt", "viewer/VMS.exe", "THIRD_PARTY_NOTICES.txt", "release.json"):
        assert (v / rel).is_file(), rel
    # el .pth del runtime se conserva (sin él, Python no encuentra site-packages)
    assert (v / "runtime" / "Lib" / "site-packages" / "distutils-precedence.pth").is_file()
    # sin cachés del desarrollador ni pesos de exportación
    assert not list(v.rglob(".DS_Store"))
    assert not (v / "models" / "weights").exists()
    assert not res.runtime_is_stub


def test_pyc_are_unchecked_hash_with_stable_path(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    res = build_layout(_inputs(artifacts), tmp_path / "out")
    pyc = next((res.version_dir / "app" / "vms" / "__pycache__").glob("__init__.cpython-312.pyc"))
    flags = struct.unpack("<I", pyc.read_bytes()[4:8])[0]
    assert flags == 0b01, "hash sin comprobar: PYTHONDONTWRITEBYTECODE + carpetas inmutables (§1.2)"
    assert b"/Users/" not in pyc.read_bytes() and b"\\Users\\" not in pyc.read_bytes()


def test_components_have_manifest_and_fixed_metadata(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    res = build_layout(_inputs(artifacts), tmp_path / "out")
    for name, comp in res.components.items():
        zpath = res.out / str(comp["file"])
        assert sha256_file(zpath) == comp["sha256"]
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            assert names == sorted(names), f"{name}: orden no determinista"
            assert "MANIFEST.sha256" in names
            infos = zf.infolist()
            assert {i.date_time for i in infos} == {(2025, 10, 5, 0, 0, 0)}
            assert {i.external_attr for i in infos} == {0x20}
            manifest = zf.read("MANIFEST.sha256").decode()
            for line in manifest.splitlines():
                digest, rel = line.split("  ", 1)
                import hashlib
                assert hashlib.sha256(zf.read(rel)).hexdigest() == digest, rel
    release = json.loads((res.version_dir / "release.json").read_text(encoding="utf-8"))
    assert release["schema"] == 1 and release["version"] == "2.0.0-ci.7"
    assert release["x-build"]["unsigned"] is True
    assert release["components"]["engine"]["restart"] == ["VMSEngine"]
    assert release["components"]["engine"]["recording_gap"] is True


def test_two_clean_builds_are_byte_identical(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    """Criterio 1 de B3 (alcance de §4.1): mismo SHA-256 del payload Python/web en dos builds."""
    build_layout(_inputs(artifacts), tmp_path / "a")
    build_layout(_inputs(artifacts), tmp_path / "b")
    assert compare_builds(tmp_path / "a", tmp_path / "b") == {}
    assert compare_builds(tmp_path / "a", tmp_path / "b", names=("engine", "viewer")) == {}
    ma = json.loads((tmp_path / "a" / "payload-manifest.json").read_text(encoding="utf-8"))
    mb = json.loads((tmp_path / "b" / "payload-manifest.json").read_text(encoding="utf-8"))
    assert ma == mb


def test_a_source_change_changes_the_app_hash(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    a = build_layout(_inputs(artifacts), tmp_path / "a")
    b = build_layout(_inputs(artifacts, epoch=EPOCH + 86400), tmp_path / "b")
    assert a.components["app"]["sha256"] != b.components["app"]["sha256"]
    assert set(compare_builds(tmp_path / "a", tmp_path / "b")) == set(REPRODUCIBLE)


def test_stub_runtime_is_marked(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    res = build_layout(_inputs(artifacts, runtime_dir=None), tmp_path / "out")
    assert res.runtime_is_stub
    assert (res.version_dir / "runtime" / "RUNTIME-DE-PRUEBA.txt").is_file()
    assert res.components["runtime"]["version"] == "stub"


def test_payload_manifest_lists_every_file(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    res = build_layout(_inputs(artifacts), tmp_path / "out")
    manifest = json.loads((res.out / "payload-manifest.json").read_text(encoding="utf-8"))
    on_disk = {f.relative_to(res.payload).as_posix() for f in res.payload.rglob("*") if f.is_file()}
    assert set(manifest["files"]) == on_disk
    assert manifest["files"]["bin/vmshost.exe"]["sha256"] == sha256_file(res.payload / "bin" / "vmshost.exe")


@pytest.mark.parametrize("missing", ["vmshost", "vmsctl", "viewer"])
def test_missing_binary_is_a_clear_error(tmp_path: Path, artifacts: dict[str, Path], missing: str) -> None:
    with pytest.raises(LayoutError, match="Falta"):
        build_layout(_inputs(artifacts, **{missing: tmp_path / "no-existe.exe"}), tmp_path / "out")


def test_engine_license_is_mandatory(tmp_path: Path, artifacts: dict[str, Path]) -> None:
    (artifacts["engine"] / "LICENSE").unlink()
    with pytest.raises(LayoutError, match="licencia de MediaMTX"):
        build_layout(_inputs(artifacts), tmp_path / "out")


def test_cli(tmp_path: Path, artifacts: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    from distribution.layout import main

    b = artifacts["bins"]
    code = main(["--version", "2.0.0", "--out", str(tmp_path / "o"), "--vmshost", str(b / "vmshost.exe"),
                 "--vmsctl", str(b / "vmsctl.exe"), "--viewer", str(b / "VMS.exe"),
                 "--engine-dir", str(artifacts["engine"]), "--epoch", str(EPOCH)])
    assert code == 0
    out = capsys.readouterr().out
    assert "runtime" in out and "AVISO: runtime de prueba" in out
    assert main(["--version", "dos", "--out", str(tmp_path / "x"), "--vmshost", "a", "--vmsctl", "b",
                 "--viewer", "c", "--engine-dir", "d"]) == 2
