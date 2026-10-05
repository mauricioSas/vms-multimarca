"""Segunda capa Authenticode (PLAN-V2 §1.6 y §4.4): sujeto y CA, nunca la huella."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vms_updater.authenticode import (AuthenticodeError, SignatureInfo, check_policy, system_verifier,
                                      verify_tree)
from vms_updater.state_files import Blacklist

from .conftest import Site

POLICY = {"subject_o": "Unmanned Studio SL", "subject_c": "ES", "issuers": ["Certum Code Signing 2021 CA"]}


def sig(o: str = "Unmanned Studio SL", c: str = "ES", issuer: str = "Certum Code Signing 2021 CA",
        thumb: str = "aa" * 32, trusted: bool = True, ts: bool = True) -> SignatureInfo:
    return SignatureInfo(trusted, o, c, issuer, thumb, ts)


def test_renewed_certificate_and_rollback_to_old_signature_both_accepted() -> None:
    old = sig(thumb="11" * 32)
    renewed = sig(thumb="22" * 32)
    check_policy(old, rel="2.0.0/bin/vmsctl.exe", **POLICY)
    check_policy(renewed, rel="2.1.0/bin/vmsctl.exe", **POLICY)


@pytest.mark.parametrize("bad,why", [
    (None, "sin firma"), (sig(trusted=False), "no válida"), (sig(ts=False), "sello de tiempo"),
    (sig(o="Otra Empresa SL"), "firmado por"), (sig(issuer="CA desconocida"), "emisor"),
    (sig(c="FR"), "firmado por"),
])
def test_policy_rejections(bad: SignatureInfo | None, why: str) -> None:
    with pytest.raises(AuthenticodeError, match=why):
        check_policy(bad, rel="x.exe", **POLICY)


def test_third_party_publisher_only_if_listed() -> None:
    psf = sig(o="Python Software Foundation", issuer="DigiCert")
    check_policy(psf, third_party=["Python Software Foundation"], rel="runtime/python.exe", **POLICY)
    with pytest.raises(AuthenticodeError):
        check_policy(psf, third_party=[], rel="runtime/python.exe", **POLICY)


def test_invalid_signature_in_new_version_is_rejected_and_nothing_changes(site: Site) -> None:
    """Un .exe con firma inválida dentro de un target con hash correcto: lo rechaza la segunda capa."""
    f = site.factory
    f.release("2.1.0", comps=("app",), authenticode=dict(POLICY))

    def verifier(p: Path) -> SignatureInfo | None:
        return sig(trusted=False) if p.name == "vmsctl.exe" else sig()

    out = site.engine(verifier=verifier).check()
    assert out.result == "update_failed" and "firma" in out.message_es
    assert site.pointer().active == "2.0.0"
    assert not site.layout.version_dir("2.1.0").exists()
    assert Blacklist(site.layout.blacklist_file).contains("2.1.0")


def test_valid_signatures_pass(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), authenticode=dict(POLICY))
    assert site.engine(verifier=lambda p: sig()).check().result == "update_ok"


def test_unsigned_release_refused_when_required(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    out = site.engine(require_authenticode=True).check()
    assert out.result == "error" and "no está firmada" in out.message_es


def test_verify_tree_skips_non_pe(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.dll").write_bytes(b"no es PE")
    (tmp_path / "c.exe").write_bytes(b"MZ...")
    seen: list[str] = []

    def v(p: Path) -> SignatureInfo:
        seen.append(p.name)
        return sig()

    assert verify_tree(tmp_path, ["a.txt", "b.dll", "c.exe"], v, **POLICY) == 1
    assert seen == ["c.exe"]


@pytest.mark.skipif(sys.platform != "win32", reason="WinVerifyTrust solo existe en Windows (job B4 de CI)")
def test_windows_verifier_on_real_binaries(tmp_path: Path) -> None:  # pragma: no cover - Windows
    v = system_verifier()
    assert v is not None
    info = v(Path(sys.executable))
    # python.exe de python.org viene firmado por la PSF (en el runner de GitHub, setup-python)
    if info is not None:
        assert info.trusted and info.subject_o == "Python Software Foundation" and info.timestamped
        check_policy(info, third_party=["Python Software Foundation"], rel="python.exe", **POLICY)
    unsigned = tmp_path / "x.exe"
    unsigned.write_bytes(b"MZ" + b"\0" * 64)
    assert v(unsigned) is None
