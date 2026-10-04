"""Regresión: secret.key se escribe de forma atómica y una clave vacía o dañada da un error claro."""
from __future__ import annotations

from pathlib import Path

import pytest

from vms.core.credentials import CredentialError, CredentialStore, EncryptedFileBackend


def test_empty_key_without_stored_passwords_is_regenerated(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "secret.key").write_bytes(b"")          # corte de luz justo tras crearla
    store = CredentialStore.create("file", secrets_dir)
    store.set_device_password("dev-00000001", "Cl@ve")
    assert store.get_device_password("dev-00000001") == "Cl@ve"
    assert len((secrets_dir / "secret.key").read_bytes().strip()) == 44


def test_damaged_key_with_stored_passwords_gives_clear_error(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    store = CredentialStore.create("file", secrets_dir)
    store.set_device_password("dev-00000001", "Cl@ve")
    (secrets_dir / "secret.key").write_bytes(b"")
    with pytest.raises(CredentialError, match="vacía o dañada"):
        CredentialStore.create("file", secrets_dir)


def test_invalid_explicit_key_is_a_credential_error(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="no es válida"):
        CredentialStore.create("file", tmp_path, secret_key="no-es-una-clave")


def test_key_is_written_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vms.core.credentials as cmod

    calls: list[Path] = []
    real = cmod.atomic_write_bytes

    def spy(path: Path, data: bytes) -> None:
        calls.append(path)
        real(path, data)

    monkeypatch.setattr(cmod, "atomic_write_bytes", spy)
    EncryptedFileBackend.load_or_create_key(tmp_path / "s")
    assert calls == [tmp_path / "s" / "secret.key"]
    assert not list((tmp_path / "s").glob("*.tmp-*"))
