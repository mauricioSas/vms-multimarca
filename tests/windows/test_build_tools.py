"""``python -m tools.build``: SBOM, firma (hueco preparado, decisión N1), ISCC y órdenes (PLAN-V2 §1.6 y §1.8)."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from tools.build import INNO_SHA256, INNO_URL, INNO_VERSION, ISS_FILE
from tools.build.__main__ import main
from tools.build.inno import iscc_command, iscc_defines
from tools.build.sbom import build_sbom
from tools.build.sign import config_from_env, has_authenticode_signature, sign_tree, unsigned_pe_files


def _pe(path: Path, *, signed: bool, pe32plus: bool = True) -> Path:
    """PE mínimo con la tabla de certificados (directorio 4) vacía o no."""
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    opt = 0x80 + 24
    struct.pack_into("<H", data, opt, 0x20B if pe32plus else 0x10B)
    dd = opt + (144 if pe32plus else 128)
    struct.pack_into("<II", data, dd, 0x100 if signed else 0, 64 if signed else 0)
    path.write_bytes(bytes(data))
    return path


def test_inno_is_pinned() -> None:
    assert INNO_VERSION == "7.1.0"
    assert INNO_URL.endswith("/is-7_1_0/innosetup-7.1.0-x64.exe")
    assert len(INNO_SHA256) == 64 and int(INNO_SHA256, 16)


def test_authenticode_detection(tmp_path: Path) -> None:
    assert has_authenticode_signature(_pe(tmp_path / "a.exe", signed=True))
    assert not has_authenticode_signature(_pe(tmp_path / "b.exe", signed=False))
    assert has_authenticode_signature(_pe(tmp_path / "c.dll", signed=True, pe32plus=False))
    (tmp_path / "d.pyd").write_bytes(b"no es un PE")
    assert not has_authenticode_signature(tmp_path / "d.pyd")
    assert [p.name for p in unsigned_pe_files(tmp_path)] == ["b.exe", "d.pyd"]


def test_without_certificate_nothing_is_signed(tmp_path: Path) -> None:
    _pe(tmp_path / "vmsctl.exe", signed=False)
    status, files = sign_tree(tmp_path, env={})
    assert status == "skipped" and [f.name for f in files] == ["vmsctl.exe"]
    assert config_from_env({}) is None


def test_jsign_config_never_puts_the_password_on_the_command_line() -> None:
    env = {"VMS_JSIGN_JAR": "jsign.jar", "VMS_SIGN_STORETYPE": "PIV", "VMS_SIGN_KEYSTORE": "x",
           "VMS_SIGN_ALIAS": "AUTHENTICATION", "VMS_SIGN_STOREPASS": "secreto", "VMS_JAVA": "java"}
    cfg = config_from_env(env)
    assert cfg is not None and cfg.tool == "jsign"
    assert "secreto" not in " ".join(cfg.command_prefix)
    assert "env:VMS_SIGN_STOREPASS" in cfg.command_prefix
    assert "--tsaurl" in cfg.command_prefix and "RFC3161" in cfg.command_prefix   # sello de tiempo (§1.6)
    with pytest.raises(ValueError, match="faltan"):
        config_from_env({"VMS_JSIGN_JAR": "jsign.jar"})


def test_dry_run_plans_but_does_not_sign(tmp_path: Path) -> None:
    _pe(tmp_path / "VMS.exe", signed=False)
    env = {"VMS_JSIGN_JAR": "jsign.jar", "VMS_SIGN_STORETYPE": "PKCS12", "VMS_SIGN_KEYSTORE": "k.p12",
           "VMS_SIGN_ALIAS": "a"}
    status, files = sign_tree(tmp_path, env=env, dry_run=True)
    assert status == "planned" and len(files) == 1


def test_iscc_command(tmp_path: Path) -> None:
    defines = iscc_defines(version="2.0.0-ci.3", numeric="2.0.0.0", payload=tmp_path / "p", output_dir=tmp_path,
                           base_name="VMSMultimarca-Setup-2.0.0-ci.3", test_build=True, sign_tool="vmssign")
    cmd = iscc_command(Path("ISCC.exe"), defines, sign_tools={"vmssign": "py -m tools.build sign-file $f"})
    assert cmd[1] == "/Qp" and cmd[-1] == str(ISS_FILE)
    assert "/DAppVersion=2.0.0-ci.3" in cmd and "/DTestBuild=1" in cmd and "/DSignToolName=vmssign" in cmd
    assert "/Svmssign=py -m tools.build sign-file $f" in cmd
    release = iscc_defines(version="2.0.0", numeric="2.0.0.0", payload=tmp_path, output_dir=tmp_path,
                           base_name="x", test_build=False)
    assert "TestBuild" not in release and "SignToolName" not in release


def test_sbom_is_deterministic_cyclonedx() -> None:
    a = build_sbom("2.0.0", 1_759_622_400)
    b = build_sbom("2.0.0", 1_759_622_400)
    assert a == b
    assert a["bomFormat"] == "CycloneDX" and a["specVersion"] == "1.6"
    assert a["metadata"]["timestamp"] == "2025-10-05T00:00:00Z"  # type: ignore[index]
    purls = {c.get("purl") for c in a["components"]}  # type: ignore[union-attr]
    assert any(str(p).startswith("pkg:pypi/fastapi@") for p in purls)
    assert any(str(p).startswith("pkg:github/bluenviron/mediamtx@") for p in purls)
    assert any(str(p).startswith("pkg:cargo/") for p in purls)
    names = [c["name"] for c in a["components"]]  # type: ignore[union-attr]
    assert "av" not in names, "PyAV (FFmpeg GPL) nunca se distribuye"


def test_cli_sbom_and_sign(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "sbom.json"
    assert main(["sbom", "--version", "2.0.0", "--out", str(out), "--epoch", "1759622400"]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["metadata"]["component"]["version"] == "2.0.0"
    _pe(tmp_path / "x.exe", signed=False)
    assert main(["sign", "--dist", str(tmp_path)]) == 0
    assert "decisión N1" in capsys.readouterr().out
    assert main(["all", "--version", "no-semver", "--skip-installer"]) == 2
