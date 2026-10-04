"""Proveedor LLM configurable para REDACTAR el informe semanal.

El modelo de lenguaje no calcula nada: recibe las cifras ya calculadas en SQL (solo agregados
anónimos, sin imágenes ni datos personales) y escribe un resumen legible para el responsable de
tienda. Después comprobamos que cada número que cite exista en esas cifras.

Configuración (.env):
    VMS_LLM_PROVIDER=anthropic | none
    VMS_LLM_MODEL=claude-sonnet-5-5          (vacío = DEFAULT_ANTHROPIC_MODEL)
    VMS_LLM_API_KEY=...

Para añadir otro proveedor basta con una clase que cumpla `LlmProvider` y un caso más en
`provider_from_settings`.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from vms.core.rtsp import redact
from vms.core.settings import VmsSettings

log = logging.getLogger("analytics.reports.llm")

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"


class LlmError(Exception):
    """El proveedor no pudo redactar (red, cuota, clave, rechazo...). Mensaje sin secretos."""


class LlmProvider(Protocol):
    name: str
    model: str

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1500) -> str: ...


class AnthropicProvider:
    """Proveedor «anthropic» con su SDK oficial (licencia MIT)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_ANTHROPIC_MODEL, *, timeout: float = 120.0,
                 max_retries: int = 3, client: Any = None) -> None:
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        if client is not None:
            self._client = client
        else:
            try:
                import anthropic
            except ImportError as exc:
                raise LlmError("Falta el paquete «anthropic» (extra central)") from exc
            self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1500) -> str:
        import anthropic

        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}])
        except anthropic.AuthenticationError as exc:
            raise LlmError("El proveedor LLM rechaza la clave (revisa VMS_LLM_API_KEY)") from exc
        except anthropic.NotFoundError as exc:
            raise LlmError(f"El proveedor LLM no reconoce el modelo «{self.model}» (revisa VMS_LLM_MODEL)") from exc
        except anthropic.RateLimitError as exc:
            raise LlmError("El proveedor LLM limita las peticiones (cuota); se usa la plantilla") from exc
        except anthropic.APIStatusError as exc:
            raise LlmError(redact(f"El proveedor LLM respondió {exc.status_code}: {exc.message}")) from exc
        except anthropic.APIConnectionError as exc:
            raise LlmError("Sin conexión con el proveedor LLM") from exc
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LlmError("El proveedor LLM declinó redactar el informe")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        if not text:
            raise LlmError(f"El proveedor LLM devolvió una respuesta vacía (motivo: {resp.stop_reason})")
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise LlmError("La respuesta del proveedor LLM quedó cortada (max_tokens)")
        return text


def provider_from_settings(settings: VmsSettings) -> LlmProvider | None:
    """Proveedor configurado, o None si es «none» o falta la clave (se usará la plantilla)."""
    if settings.llm_provider == "none":
        return None
    if settings.llm_provider == "anthropic":
        if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
            log.warning("VMS_LLM_PROVIDER=anthropic pero falta VMS_LLM_API_KEY: el informe usará la plantilla")
            return None
        try:
            return AnthropicProvider(settings.llm_api_key.get_secret_value(), settings.llm_model)
        except LlmError as exc:
            log.warning("%s: el informe usará la plantilla", exc)
            return None
    log.warning("Proveedor LLM desconocido: %s", settings.llm_provider)
    return None
