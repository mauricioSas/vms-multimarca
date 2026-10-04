"""Formato único de error de la API: {"error": {"code", "message", "details"}} (CONTRATO §6.1)."""
from __future__ import annotations

import logging
from typing import Any

import pydantic_core
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from vms.core.credentials import CredentialError
from vms.core.errors import RateLimited, VmsError

log = logging.getLogger("vms.api")

_FIELD_MESSAGES = {
    "missing": "Campo obligatorio",
    "string_too_short": "Texto demasiado corto",
    "string_too_long": "Texto demasiado largo",
    "too_short": "Faltan elementos",
    "too_long": "Demasiados elementos",
    "greater_than_equal": "Valor demasiado pequeño",
    "greater_than": "Valor demasiado pequeño",
    "less_than_equal": "Valor demasiado grande",
    "less_than": "Valor demasiado grande",
    "int_parsing": "Debe ser un número entero",
    "int_type": "Debe ser un número entero",
    "float_parsing": "Debe ser un número",
    "float_type": "Debe ser un número",
    "bool_parsing": "Debe ser verdadero o falso",
    "bool_type": "Debe ser verdadero o falso",
    "string_type": "Debe ser un texto",
    "literal_error": "Valor no permitido",
    "enum": "Valor no permitido",
    "string_pattern_mismatch": "Formato no válido",
    "datetime_parsing": "Fecha no válida (usa ISO 8601)",
    "datetime_from_date_parsing": "Fecha no válida (usa ISO 8601)",
    "json_invalid": "JSON no válido",
    "model_attributes_type": "Debe ser un objeto JSON",
    "dict_type": "Debe ser un objeto JSON",
    "list_type": "Debe ser una lista",
    "tuple_type": "Debe ser una lista",
    "union_tag_invalid": "Tipo no válido",
    "union_tag_not_found": "Falta el tipo («kind»)",
}


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    """JSON con fechas ISO 8601 en UTC con «Z» (serializador de pydantic) y sin secretos (SecretStr → ***)."""
    return Response(pydantic_core.to_json(data), status_code=status, media_type="application/json",
                    headers=headers)


def error_response(code: str, message: str, status: int, details: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> Response:
    return json_response({"error": {"code": code, "message": message, "details": details or {}}}, status, headers)


def field_errors(errors: list[Any]) -> list[dict[str, Any]]:
    out = []
    for e in errors:
        loc = [x if isinstance(x, int) else str(x) for x in e.get("loc", ()) if x not in ("body",)]
        etype = str(e.get("type", ""))
        msg = str(e.get("msg", ""))
        if etype == "value_error":
            msg = msg.removeprefix("Value error, ")
        elif etype == "assertion_error":
            msg = msg.removeprefix("Assertion failed, ")
        else:
            msg = _FIELD_MESSAGES.get(etype, msg)
        out.append({"loc": loc, "msg": msg})
    return out


def validation_response(errors: list[Any], message: str = "Hay datos no válidos") -> Response:
    return error_response("validation_error", message, 422, {"fields": field_errors(errors)})


def install(app: FastAPI) -> None:
    async def vms_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, VmsError)
        headers = None
        if isinstance(exc, RateLimited) and exc.details.get("retry_after"):
            headers = {"Retry-After": str(int(exc.details["retry_after"]))}
        if exc.status >= 500:
            log.warning("%s %s → %s: %s", request.method, request.url.path, exc.code, exc.message)
        return error_response(exc.code, exc.message, exc.status, exc.details, headers)

    async def request_validation(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        return validation_response(list(exc.errors()))

    async def model_validation(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ValidationError)
        return validation_response(exc.errors(include_url=False))  # type: ignore[arg-type]

    async def credential_error(request: Request, exc: Exception) -> Response:
        log.error("Almacén de credenciales: %s", exc)
        return error_response("credential_store_error", f"No se pudo usar el almacén de contraseñas: {exc}", 503)

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        codes = {404: ("not_found", "No encontrado"), 405: ("method_not_allowed", "Método no permitido"),
                 401: ("unauthorized", "Inicia sesión"), 403: ("forbidden", "No tienes permiso"),
                 413: ("too_large", "Petición demasiado grande"), 415: ("unsupported_media_type",
                                                                         "Tipo de contenido no admitido")}
        code, msg = codes.get(exc.status_code, ("http_error", str(exc.detail)))
        if isinstance(exc.detail, str) and exc.status_code not in (404, 405):
            msg = exc.detail
        return error_response(code, msg, exc.status_code, headers=getattr(exc, "headers", None))

    async def unhandled(request: Request, exc: Exception) -> Response:
        log.exception("Error no controlado en %s %s", request.method, request.url.path)
        return error_response("internal_error", "Error interno del servidor; revisa el registro", 500)

    app.add_exception_handler(VmsError, vms_error)
    app.add_exception_handler(RequestValidationError, request_validation)
    app.add_exception_handler(ValidationError, model_validation)
    app.add_exception_handler(CredentialError, credential_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(Exception, unhandled)
