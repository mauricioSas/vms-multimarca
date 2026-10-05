"""Campos del latido que aportan otros bloques (CONTRATO §7.3, §15.6, §18.5 y §18.7). Dueño: arquitecto.

El latido sale por dos caminos: directo desde el backend (`AppState.heartbeat_payload`) y por el agente
de sede (`central/agent.py`). Los dos llaman a `collect_extras(paths)` desde la fase 0 de la v2, así que
cada bloque escribe **solo su proveedor, en su propia carpeta**, y nadie toca `central/agent.py`,
`central/heartbeat.py` ni `vms/api/state.py`. Aquí solo se declara qué clave aporta cada uno:

| Clave del latido | Proveedor (`módulo:función`)              | Bloque |
|------------------|-------------------------------------------|--------|
| `update`         | `vms_updater.heartbeat:payload_update`    | B4 (lee `updater\\public-status.json`, §15.6) |
| `health`         | `vms.ops.heartbeat:payload_health`        | B6 (§18.5) |
| `evidence_key`   | `vms.ops.heartbeat:payload_evidence_key`  | B6 (§18.7) |

Un proveedor es `def f(paths: AppPaths) -> dict[str, Any] | None`: síncrono, rápido, solo lee archivos
locales (nunca red ni la base de datos), sin imágenes, IP ni secretos. Si su módulo aún no existe, si lanza
o si devuelve algo que no es un objeto JSON de como mucho `MAX_EXTRA_BYTES`, esa clave no se envía y el
resto del latido sale igual. `HeartbeatPayload` ya admite campos extra (`extra="allow"`).
"""
from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable
from typing import Any

from .paths import AppPaths

log = logging.getLogger("vms.heartbeat.extras")

Provider = Callable[[AppPaths], "dict[str, Any] | None"]

PROVIDERS: dict[str, str] = {
    "update": "vms_updater.heartbeat:payload_update",
    "health": "vms.ops.heartbeat:payload_health",
    "evidence_key": "vms.ops.heartbeat:payload_evidence_key",
}
MAX_EXTRA_BYTES = 8 * 1024


def _resolve(spec: str) -> Provider | None:
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        missing = exc.name or ""
        if missing and (module_name == missing or module_name.startswith(missing + ".")):
            log.debug("Proveedor del latido %s aún no disponible", spec)   # el bloque no lo ha entregado
        else:
            log.warning("No se pudo importar el proveedor del latido %s: %s", spec, exc)
        return None
    fn = getattr(module, attr, None)
    if not callable(fn):
        log.debug("El módulo %s no tiene %s", module_name, attr)
        return None
    provider: Provider = fn
    return provider


def collect_extras(paths: AppPaths) -> dict[str, Any]:
    """Claves extra del latido que hayan entregado los bloques (las que fallen se omiten)."""
    out: dict[str, Any] = {}
    for key, spec in PROVIDERS.items():
        fn = _resolve(spec)
        if fn is None:
            continue
        try:
            value = fn(paths)
        except Exception:  # noqa: BLE001 - un proveedor roto no puede tumbar el latido
            log.exception("El proveedor del latido %s falló", spec)
            continue
        if value is None:
            continue
        if not isinstance(value, dict):
            log.warning("El proveedor del latido %s no devolvió un objeto: se omite", spec)
            continue
        try:
            size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            log.warning("El proveedor del latido %s devolvió algo que no es JSON: se omite", spec)
            continue
        if size > MAX_EXTRA_BYTES:
            log.warning("«%s» del latido ocupa %d bytes (máx. %d): se omite", key, size, MAX_EXTRA_BYTES)
            continue
        out[key] = value
    return out
