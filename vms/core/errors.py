"""Errores de dominio con código estable y estado HTTP.

La API los convierte en {"error": {"code", "message", "details"}} (ver docs/CONTRATO.md §6.1).
`message` siempre va en español y es apto para mostrarse al usuario.
"""
from __future__ import annotations

from typing import Any


class VmsError(Exception):
    code = "error"
    status = 500

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class ValidationFailed(VmsError):
    code, status = "validation_error", 422


class NotFoundError(VmsError):
    code, status = "not_found", 404


class ConflictError(VmsError):
    code, status = "conflict", 409


class AuthError(VmsError):
    code, status = "unauthorized", 401


class ForbiddenError(VmsError):
    code, status = "forbidden", 403


class RateLimited(VmsError):
    code, status = "rate_limited", 429


class NotConfigured(VmsError):
    code, status = "not_configured", 503


class EngineUnavailable(VmsError):
    """El motor de vídeo no responde (caído, arrancando o sin el servicio activado)."""

    code, status = "engine_unavailable", 503


class EngineError(VmsError):
    """El motor de vídeo responde, pero con un error (permisos, credenciales internas, carpeta que falta…).

    Estado 502, como `device_error`: el fallo es de lo que hay detrás del backend, no de la petición."""

    code, status = "engine_error", 502


class DeviceError(VmsError):
    """Fallo al hablar con una cámara o NVR. Estado 502: el fallo es del equipo, no de la sesión."""

    code, status = "device_error", 502


class DeviceUnreachable(DeviceError):
    code = "device_unreachable"


class DeviceAuthFailed(DeviceError):
    code = "device_auth_failed"


class DeviceProtocolError(DeviceError):
    code = "device_protocol_error"


class DeviceUnsupported(DeviceError):
    code, status = "device_unsupported", 501
