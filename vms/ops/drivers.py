"""Lo que B6 pregunta al registro de drivers de B5 (CONTRATO §16.1), sin importarlo de forma estática.

B6 no decide por marca con cadenas de `if` ni con `isinstance` sobre el cliente: pregunta al registro qué sabe
hacer el driver del equipo (`time_read`, `security_read`) y si tiene API (cliente propio u ONVIF). Un equipo sin
API (Ezviz, Reolink, «Genérico (RTSP manual)») no tiene cliente: `client_for()` lanzaría `DeviceUnsupported`, así
que B6 ni lo pide (la salud usa el fotograma del RTSP local, la hora y la auditoría lo dicen en el resultado).

El registro se carga con `dynamic()` para que `mypy vms/ops` no revise `vms.vendors` (tiene su propia revisión
estricta en CI).
"""
from __future__ import annotations

import logging
from typing import Any

from .host import dynamic

log = logging.getLogger("vms.ops.drivers")

TIME_READ = "time_read"
SECURITY_READ = "security_read"
GENERIC_DRIVERS = frozenset({"onvif", "generic"})   # no dicen la marca real del equipo


def spec_of(vendor: str) -> Any | None:
    """`DriverSpec` del registro de B5 (None si la marca no existe o el registro no se puede cargar)."""
    try:
        return dynamic("vms.vendors.registry", "get_driver")(vendor)
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 - un registro roto no tumba la operación
        log.debug("Registro de drivers no disponible", exc_info=True)
        return None


def capabilities(vendor: str) -> frozenset[str]:
    """Capacidades declaradas por el driver («time_read», «security_read»…); vacío si no se conoce."""
    spec = spec_of(vendor)
    if spec is None:
        return frozenset()
    return frozenset(str(c) for c in spec.capabilities)


def has_api(vendor: str) -> bool:
    """¿El driver tiene cliente de API (propio u ONVIF)? Sin API, `client_for()` no da cliente."""
    spec = spec_of(vendor)
    if spec is None:
        return False
    try:
        return bool(dynamic("vms.vendors", "has_api")(spec))
    except Exception:  # noqa: BLE001
        log.debug("has_api no disponible", exc_info=True)
        return False


def driver_name(vendor: str) -> str:
    spec = spec_of(vendor)
    return str(spec.name) if spec is not None else vendor


def brand_from(vendor: str, manufacturer: str = "", model: str = "") -> str | None:
    """Marca real del equipo para la tabla de avisos.

    Con un driver de marca (Hikvision, Dahua, Uniview…) es la del driver. Con un driver genérico («ONVIF (otras
    marcas)» o RTSP manual) se deduce del fabricante que dio el equipo y del modelo con el detector de B5
    (`best_match`); si no se puede deducir con seguridad, None (la auditoría dirá «Desconocido», nunca «ok»)."""
    if vendor not in GENERIC_DRIVERS:
        return vendor if spec_of(vendor) is not None else None
    if not (manufacturer or model):
        return None
    try:
        hints = dynamic("vms.core.interfaces", "DetectionHints")(manufacturer=manufacturer, model=model)
        matches = dynamic("vms.vendors.registry", "best_match")(hints)
    except Exception:  # noqa: BLE001
        log.debug("No se pudo deducir la marca", exc_info=True)
        return None
    for spec, score in matches:
        if spec.id in GENERIC_DRIVERS:
            continue
        return str(spec.id) if score >= 0.5 else None
    return None


__all__ = ["GENERIC_DRIVERS", "SECURITY_READ", "TIME_READ", "brand_from", "capabilities", "driver_name",
           "has_api", "spec_of"]
