"""DPAPI de máquina para `secrets\\` (CONTRATO §13.9) y migración del `secret.key` en claro de la v1.

Fuera de Windows se prueba la lógica (formato, migración, almacén de credenciales) con un DPAPI simulado; el
DPAPI real lo prueba el job de Windows de B1 (`pytest tests/core/test_winsec.py` en windows-latest).
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from vms.core import winsec
from vms.core.credentials import CredentialStore, EncryptedFileBackend

FAKE_MARK = b"FAKE-DPAPI:"


@pytest.fixture
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """DPAPI de mentira, reversible y que no deja ver el secreto (para probar la lógica en macOS/Linux)."""
    def fake(data: bytes, *, protect: bool) -> bytes:
        if protect:
            return FAKE_MARK + bytes(b ^ 0x5A for b in data)
        if not data.startswith(FAKE_MARK):
            raise winsec.SecretProtectionError("blob de otro equipo")
        return bytes(b ^ 0x5A for b in data[len(FAKE_MARK):])

    monkeypatch.setattr(winsec, "_dpapi", fake)
    monkeypatch.setattr(winsec, "dpapi_available", lambda: True)


def test_format_roundtrip_and_plaintext_detection() -> None:
    raw = winsec.encode_protected(b"\x01\x02\x03")
    assert raw == b"vms-dpapi-v1:AQID\n"   # mismo formato que vms_common::secret::format_protected
    assert winsec.parse(raw) == (True, b"\x01\x02\x03")
    assert winsec.parse(b"token-de-la-v1") == (False, b"token-de-la-v1")
    assert winsec.parse(b"vms-dpapi-v1:%%%") == (False, b"vms-dpapi-v1:%%%")


def test_service_sid_matches_windows_and_rust() -> None:
    assert winsec.service_sid("TrustedInstaller") == \
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    assert winsec.service_sid("vmsbackend") == winsec.service_sid("VMSBackend")


@pytest.mark.skipif(sys.platform == "win32", reason="fuera de Windows no hay DPAPI")
def test_without_dpapi_secrets_stay_plain_with_owner_only_permissions(tmp_path: Path) -> None:
    p = tmp_path / "secrets" / "internal.token"
    winsec.write_secret(p, b"abc")
    assert p.read_bytes() == b"abc"
    assert p.stat().st_mode & 0o077 == 0
    assert winsec.read_secret_text(p) == "abc"
    assert winsec.migrate_plaintext(p) is False
    with pytest.raises(winsec.SecretProtectionError):
        winsec.protect(b"x")


@pytest.mark.usefixtures("fake_dpapi")
def test_write_read_and_migrate_with_dpapi(tmp_path: Path) -> None:
    p = tmp_path / "kiosk.token"
    winsec.write_secret(p, b"s3cr3t")
    assert p.read_bytes().startswith(winsec.PREFIX) and b"s3cr3t" not in p.read_bytes()
    assert winsec.read_secret(p) == b"s3cr3t"
    assert winsec.is_protected(p)

    old = tmp_path / "site.token"
    old.write_bytes(b"token-v1")
    assert winsec.migrate_plaintext(old) is True
    assert b"token-v1" not in old.read_bytes()
    assert winsec.read_secret(old) == b"token-v1"
    assert winsec.migrate_plaintext(old) is False, "la segunda vez no hace nada"


@pytest.mark.usefixtures("fake_dpapi")
def test_v1_secret_key_is_protected_on_load_and_passwords_still_decrypt(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = Fernet.generate_key()
    (secrets / "secret.key").write_bytes(key)                     # v1: clave en claro
    v1 = CredentialStore(EncryptedFileBackend(secrets / "credentials.enc", key))
    v1.set_device_password("dev-1", "Cl4ve!")
    store = CredentialStore.create("file", secrets)
    assert store.get_device_password("dev-1") == "Cl4ve!"
    raw = (secrets / "secret.key").read_bytes()
    assert raw.startswith(winsec.PREFIX) and key not in raw, "la clave queda protegida con DPAPI"
    # Y se sigue abriendo en el siguiente arranque
    assert CredentialStore.create("file", secrets).get_device_password("dev-1") == "Cl4ve!"


@pytest.mark.usefixtures("fake_dpapi")
def test_new_key_is_created_protected(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    store = CredentialStore.create("file", secrets)
    store.set_device_password("dev-2", "x")
    assert winsec.is_protected(secrets / "secret.key")


@pytest.mark.usefixtures("fake_dpapi")
def test_key_from_another_machine_is_a_clear_error(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "secret.key").write_bytes(winsec.PREFIX + base64.b64encode(b"de-otro-pc") + b"\n")
    with pytest.raises(Exception, match="equipo que lo creó"):
        CredentialStore.create("file", secrets)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI real solo en Windows")
def test_real_dpapi_roundtrip_on_windows(tmp_path: Path) -> None:  # pragma: no cover
    blob = winsec.protect(b"s3cr3t")
    assert b"s3cr3t" not in blob
    assert winsec.unprotect(blob) == b"s3cr3t"
    p = tmp_path / "secret.key"
    winsec.write_secret(p, b"clave")
    assert winsec.is_protected(p) and winsec.read_secret(p) == b"clave"
    with pytest.raises(winsec.SecretProtectionError):
        winsec.unprotect(b"no es un blob de DPAPI")
    old = tmp_path / "internal.token"
    old.write_bytes(b"token-v1")
    assert winsec.migrate_plaintext(old) and winsec.read_secret(old) == b"token-v1"



def _v1_store(tmp_path: Path) -> tuple[Path, bytes]:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = Fernet.generate_key()
    (secrets / "secret.key").write_bytes(key)                     # v1: clave en claro
    CredentialStore(EncryptedFileBackend(secrets / "credentials.enc", key)).set_device_password("dev-1", "Cl4ve!")
    return secrets, key


@pytest.mark.usefixtures("fake_dpapi")
def test_failed_replace_keeps_the_v1_key_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresión: si la sustitución falla (antivirus, disco lleno), la clave en claro NO se pierde."""
    secrets, key = _v1_store(tmp_path)

    def locked(path: Path, data: bytes) -> None:
        raise PermissionError(13, "bloqueado por el antivirus")

    with monkeypatch.context() as m:
        m.setattr(winsec, "atomic_write_bytes", locked)
        with pytest.raises(PermissionError):
            winsec.migrate_plaintext(secrets / "secret.key")
        assert (secrets / "secret.key").read_bytes() == key, "la clave en claro sigue intacta"
        # El almacén abre igual (la migración fallida solo deja un aviso) y las contraseñas siguen ahí
        assert CredentialStore.create("file", secrets).get_device_password("dev-1") == "Cl4ve!"
        assert (secrets / "secret.key").read_bytes() == key
    # Sin el bloqueo, el siguiente arranque la migra y todo sigue abriendo
    assert CredentialStore.create("file", secrets).get_device_password("dev-1") == "Cl4ve!"
    assert winsec.is_protected(secrets / "secret.key")


@pytest.mark.usefixtures("fake_dpapi")
def test_power_cut_between_temp_and_rename_keeps_the_v1_key(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresión: corte de luz tras escribir el temporal y antes de renombrar → la clave sigue en claro."""
    from vms.core import atomic

    secrets, key = _v1_store(tmp_path)

    class PowerCut(BaseException):
        pass

    def cut(tmp: Path, dst: Path) -> None:
        raise PowerCut

    with monkeypatch.context() as m:
        m.setattr(atomic, "replace", cut)
        with pytest.raises(PowerCut):
            winsec.migrate_plaintext(secrets / "secret.key")
    assert (secrets / "secret.key").read_bytes() == key
    # «Reinicio»: el almacén abre, migra (pisando el temporal huérfano) y las contraseñas siguen
    assert CredentialStore.create("file", secrets).get_device_password("dev-1") == "Cl4ve!"
    assert winsec.is_protected(secrets / "secret.key")
    assert not atomic.temp_path(secrets / "secret.key").exists()


@pytest.mark.usefixtures("fake_dpapi")
def test_dpapi_roundtrip_mismatch_does_not_touch_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "site.token"
    p.write_bytes(b"token-v1")
    monkeypatch.setattr(winsec, "unprotect", lambda blob: b"otra-cosa")
    with pytest.raises(winsec.SecretProtectionError, match="se deja en claro"):
        winsec.migrate_plaintext(p)
    assert p.read_bytes() == b"token-v1"


@pytest.mark.usefixtures("fake_dpapi")
def test_unreadable_after_replace_restores_the_plaintext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "secret.key"
    p.write_bytes(b"clave-v1")

    def broken_read(path: Path) -> bytes:
        raise winsec.SecretProtectionError("lectura rota")

    monkeypatch.setattr(winsec, "read_secret", broken_read)
    with pytest.raises(winsec.SecretProtectionError, match="se restauró en claro"):
        winsec.migrate_plaintext(p)
    assert p.read_bytes() == b"clave-v1"
