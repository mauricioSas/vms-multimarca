from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from vms.core.credentials import (CredentialError, CredentialStore, EncryptedFileBackend, MemoryKeyring,
                                  system_keyring_available)
from vms.core.logging_setup import setup_logging
from vms.core.paths import AppPaths
from vms.core.settings import VmsSettings, load_settings


def test_encrypted_file_roundtrip(app_paths: AppPaths, credential_store: CredentialStore) -> None:
    credential_store.set_device_password("dev-12345678", "Cl@ve:compleja#1")
    assert credential_store.get_device_password("dev-12345678") == "Cl@ve:compleja#1"
    raw = (app_paths.secrets_dir / "credentials.enc").read_bytes()
    assert b"compleja" not in raw
    if sys.platform != "win32":
        assert oct((app_paths.secrets_dir / "secret.key").stat().st_mode & 0o777) == "0o600"
    credential_store.set_device_password("dev-12345678", "")
    assert credential_store.get_device_password("dev-12345678") == ""


def test_wrong_key_raises_clear_error(app_paths: AppPaths, credential_store: CredentialStore) -> None:
    credential_store.set_device_password("dev-12345678", "x")
    other = EncryptedFileBackend(app_paths.secrets_dir / "credentials.enc", Fernet.generate_key())
    with pytest.raises(CredentialError, match="clave no coincide"):
        other.get("device:dev-12345678")


def test_keyring_backend_selected_in_tests(app_paths: AppPaths) -> None:
    assert os.environ["PYTHON_KEYRING_BACKEND"].endswith("MemoryKeyring")
    assert system_keyring_available()
    store = CredentialStore.create("auto", app_paths.secrets_dir)
    assert store.backend_name == "keyring"
    store.set_device_password("dev-aaaaaaaa", "pw")
    assert store.has_device_password("dev-aaaaaaaa")
    store.delete_device_password("dev-aaaaaaaa")
    store.delete_device_password("dev-aaaaaaaa")  # borrar dos veces no falla
    assert isinstance(MemoryKeyring(), MemoryKeyring)


def test_settings_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / "x.env"
    env.write_text("VMS_HTTP_PORT=9100\nVMS_PG_DSN=postgresql://u:secreto@h/db\nVMS_DATA_DIR=\n"
                   'VMS_MTX_WEBRTC_ADDITIONAL_HOSTS=["100.64.0.2"]\n', encoding="utf-8")
    s = load_settings(env)
    assert s.http_port == 9100 and s.data_dir is None
    assert s.mtx_webrtc_additional_hosts == ["100.64.0.2"]
    assert "secreto" not in repr(s)
    assert s.mtx_rtsp_url("cam-12345678/sub") == "rtsp://127.0.0.1:8554/cam-12345678/sub"


def test_internal_token_generated_once(settings: VmsSettings, app_paths: AppPaths) -> None:
    s = VmsSettings(_env_file=None, data_dir=app_paths.base)  # type: ignore[call-arg]
    t1 = s.ensure_internal_token()
    t2 = VmsSettings(_env_file=None, data_dir=app_paths.base).ensure_internal_token()  # type: ignore[call-arg]
    assert t1 == t2 and len(t1) >= 32
    assert settings.ensure_internal_token() == "token-interno-de-pruebas"


def test_logging_redacts_messages_and_tracebacks(app_paths: AppPaths) -> None:
    setup_logging(app_paths.logs_dir, "INFO", console=False)
    log = logging.getLogger("vms.test")
    log.info("conectando a %s", "rtsp://admin:SuperSecreta@10.0.0.2:554/x")
    try:
        raise RuntimeError("fallo con http://admin:OtraClave@cam/ISAPI")
    except RuntimeError:
        log.exception("error")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (app_paths.logs_dir / "vms.log").read_text(encoding="utf-8")
    assert "SuperSecreta" not in text and "OtraClave" not in text
    assert text.count("***:***@") >= 2
    setup_logging(None, "INFO", console=False)


def test_ice_addresses_can_be_disabled_from_env_file(tmp_path: Path) -> None:
    """Vacío en el .env = valor por defecto; «off» = desactivado (CONTRATO §3.1)."""
    env = tmp_path / "x.env"
    env.write_text("VMS_MTX_WEBRTC_ICE_TCP=off\nVMS_MTX_WEBRTC_ICE_UDP=\n", encoding="utf-8")
    s = load_settings(env)
    assert s.mtx_webrtc_ice_tcp == "" and s.mtx_webrtc_ice_udp == ":8189"
