"""Contraseñas de equipos fuera de config.json.

Backends (VMS_CREDENTIAL_BACKEND):
  - keyring: almacén del sistema (Administrador de credenciales de Windows, Llavero de macOS,
    Secret Service en Linux con sesión gráfica).
  - file: archivo secrets/credentials.enc cifrado con Fernet (AES-128-CBC + HMAC-SHA256).
    La clave sale de VMS_SECRET_KEY o, si no existe, de secrets/secret.key (se genera una vez
    con permisos solo del propietario). Pensado para mini PC Linux sin sesión gráfica.
  - auto (por defecto): keyring si hay un almacén real; si no, file.

La clave de cada contraseña es el id del equipo (nunca su nombre ni su IP).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from .atomic import atomic_write_bytes, atomic_write_text
from .paths import restrict_permissions

log = logging.getLogger(__name__)

SERVICE = "VMSMultimarca"

try:
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import KeyringError, PasswordDeleteError
except ImportError:  # pragma: no cover - keyring es dependencia obligatoria
    keyring = None  # type: ignore[assignment]
    KeyringBackend = object  # type: ignore[assignment,misc]
    KeyringError = PasswordDeleteError = Exception  # type: ignore[assignment,misc]


class CredentialError(RuntimeError):
    pass


class SecretBackend(Protocol):
    name: str

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, secret: str) -> None: ...
    def delete(self, key: str) -> None: ...


class MemoryKeyring(KeyringBackend):
    """Backend de keyring en memoria para pruebas
    (PYTHON_KEYRING_BACKEND=vms.core.credentials.MemoryKeyring)."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._data:
            raise PasswordDeleteError("no existe")
        del self._data[(service, username)]


def system_keyring_available() -> bool:
    if keyring is None:
        return False
    try:
        module = type(keyring.get_keyring()).__module__
    except Exception as exc:  # backends rotos lanzan errores variados al inicializarse
        log.warning("No se pudo inicializar keyring: %s", exc)
        return False
    return not module.endswith((".fail", ".null", "chainer")) or _chainer_has_real_backend()


def _chainer_has_real_backend() -> bool:
    try:
        kr = keyring.get_keyring()
        backends = getattr(kr, "backends", [])
        return any(not type(b).__module__.endswith((".fail", ".null")) for b in backends)
    except Exception:  # noqa: BLE001 - mismo motivo que arriba
        return False


class KeyringSecretBackend:
    name = "keyring"

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    def get(self, key: str) -> str | None:
        try:
            return keyring.get_password(self.service, key)
        except KeyringError as exc:
            raise CredentialError(f"No se pudo leer del almacén del sistema: {exc}") from exc

    def set(self, key: str, secret: str) -> None:
        try:
            keyring.set_password(self.service, key, secret)
        except KeyringError as exc:
            raise CredentialError(f"No se pudo guardar en el almacén del sistema: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(self.service, key)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise CredentialError(f"No se pudo borrar del almacén del sistema: {exc}") from exc


def _valid_fernet_key(key: bytes) -> bool:
    try:
        Fernet(key)
    except (ValueError, TypeError):
        return False
    return True


class EncryptedFileBackend:
    name = "file"

    def __init__(self, file: Path, key: bytes) -> None:
        self.file = Path(file)
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise CredentialError("La clave del almacén de credenciales no es válida (VMS_SECRET_KEY o "
                                  "secrets/secret.key): debe ser una clave Fernet de 32 bytes en base64") from exc
        self._lock = threading.Lock()

    @staticmethod
    def load_or_create_key(secrets_dir: Path, explicit: str | None = None) -> bytes:
        if explicit:
            return explicit.encode("ascii")
        secrets_dir.mkdir(parents=True, exist_ok=True)
        key_file = secrets_dir / "secret.key"
        store_file = secrets_dir / "credentials.enc"
        if key_file.is_file():
            key = key_file.read_bytes().strip()
            if _valid_fernet_key(key):
                return key
            if store_file.is_file() and store_file.stat().st_size > 0:
                raise CredentialError(
                    f"La clave {key_file} está vacía o dañada y ya hay contraseñas cifradas con ella "
                    f"({store_file.name}). Restaura secret.key de una copia de seguridad, o borra los dos "
                    "archivos y vuelve a escribir la contraseña de cada equipo.")
            log.warning("La clave del almacén de credenciales estaba vacía o dañada y aún no protegía nada: "
                        "se genera una nueva")
        key = Fernet.generate_key()
        # Escritura atómica con fsync (también de la carpeta): un corte de luz justo después del primer
        # arranque no puede dejar una clave vacía con contraseñas ya cifradas con ella.
        atomic_write_bytes(key_file, key)
        restrict_permissions(key_file)
        log.info("Se generó una clave nueva para el almacén cifrado de credenciales")
        return key

    def _read_all(self) -> dict[str, str]:
        if not self.file.is_file():
            return {}
        try:
            raw = self._fernet.decrypt(self.file.read_bytes())
        except InvalidToken as exc:
            raise CredentialError(
                "No se puede descifrar el almacén de credenciales: la clave no coincide") from exc
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise CredentialError("El almacén de credenciales tiene un formato no válido")
        return {str(k): str(v) for k, v in data.items()}

    def _write_all(self, data: dict[str, str]) -> None:
        token = self._fernet.encrypt(json.dumps(data).encode("utf-8"))
        atomic_write_text(self.file, token.decode("ascii"))
        restrict_permissions(self.file)

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._read_all().get(key)

    def set(self, key: str, secret: str) -> None:
        with self._lock:
            data = self._read_all()
            data[key] = secret
            self._write_all(data)

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._read_all()
            if data.pop(key, None) is not None:
                self._write_all(data)


class CredentialStore:
    """Fachada usada por el resto del código. get() devuelve "" si no hay contraseña."""

    def __init__(self, backend: SecretBackend) -> None:
        self.backend = backend

    @classmethod
    def create(cls, mode: str, secrets_dir: Path, secret_key: str | None = None,
               service: str = SERVICE) -> "CredentialStore":
        if mode == "keyring" or (mode == "auto" and system_keyring_available()):
            if mode == "keyring" and not system_keyring_available():
                raise CredentialError("Se pidió keyring pero el sistema no tiene un almacén disponible")
            return cls(KeyringSecretBackend(service))
        key = EncryptedFileBackend.load_or_create_key(secrets_dir, secret_key)
        return cls(EncryptedFileBackend(secrets_dir / "credentials.enc", key))

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @staticmethod
    def device_key(device_id: str) -> str:
        return f"device:{device_id}"

    def get_device_password(self, device_id: str) -> str:
        return self.backend.get(self.device_key(device_id)) or ""

    def set_device_password(self, device_id: str, password: str) -> None:
        if password:
            self.backend.set(self.device_key(device_id), password)
        else:
            self.backend.delete(self.device_key(device_id))

    def delete_device_password(self, device_id: str) -> None:
        self.backend.delete(self.device_key(device_id))

    def has_device_password(self, device_id: str) -> bool:
        return bool(self.backend.get(self.device_key(device_id)))
