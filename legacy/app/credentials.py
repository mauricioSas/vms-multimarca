"""Contraseñas en el almacén seguro del sistema (Administrador de credenciales de Windows).

Se usa la biblioteca keyring. La clave es el id del dispositivo, nunca su nombre.
Si el sistema no tiene almacén disponible, la contraseña solo se guarda en memoria
durante la sesión y se avisa al usuario: NUNCA se escribe en el JSON.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SERVICE = "VMSMultimarca"

try:
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import KeyringError, PasswordDeleteError
except Exception:  # pragma: no cover
    keyring = None
    KeyringBackend = object  # type: ignore[assignment,misc]
    KeyringError = PasswordDeleteError = Exception  # type: ignore[assignment,misc]


class MemoryKeyring(KeyringBackend):  # type: ignore[misc,valid-type]
    """Backend en memoria para pruebas automáticas (PYTHON_KEYRING_BACKEND=app.credentials.MemoryKeyring)."""

    priority = 1  # type: ignore[assignment]

    def __init__(self):
        super().__init__()
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._data.get((service, username))

    def set_password(self, service, username, password):
        self._data[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self._data:
            raise PasswordDeleteError("no existe")
        del self._data[(service, username)]


class CredentialStore:
    def __init__(self, service: str = SERVICE):
        self.service = service
        self._session: dict[str, str] = {}
        self.secure = keyring is not None and self._probe()

    def _probe(self) -> bool:
        try:
            module = type(keyring.get_keyring()).__module__
            # keyring.backends.fail / null = no hay almacén real en este equipo
            return not module.endswith((".fail", ".null"))
        except Exception:
            return False

    def backend_name(self) -> str:
        if not self.secure:
            return "solo memoria (sin almacén seguro)"
        b = keyring.get_keyring()
        return f"{type(b).__module__}.{type(b).__name__}"

    def get(self, device_id: str) -> str:
        if device_id in self._session:
            return self._session[device_id]
        if not self.secure:
            return ""
        try:
            return keyring.get_password(self.service, device_id) or ""
        except KeyringError as exc:
            log.error("No se pudo leer la contraseña del almacén: %s", exc)
            return ""

    def set(self, device_id: str, password: str) -> bool:
        """Guarda la contraseña. Devuelve False si solo se pudo guardar en memoria."""
        if not self.secure:
            self._session[device_id] = password
            return False
        try:
            if password:
                keyring.set_password(self.service, device_id, password)
            else:
                self.delete(device_id)
            self._session.pop(device_id, None)
            return True
        except KeyringError as exc:
            log.error("No se pudo guardar la contraseña en el almacén: %s", exc)
            self._session[device_id] = password
            return False

    def delete(self, device_id: str) -> None:
        self._session.pop(device_id, None)
        if not self.secure:
            return
        try:
            keyring.delete_password(self.service, device_id)
        except (PasswordDeleteError, KeyringError):
            pass
