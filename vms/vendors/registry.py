"""Registro de drivers de fabricante (CONTRATO §16, PLAN-V2 §3.1).

Cada driver vive en **un archivo** `vms/vendors/drivers/<id>.py` que construye su `DriverSpec` y llama a
`register()`. Este módulo importa todos los archivos de esa carpeta al cargarse: añadir una marca no exige
tocar el registro, la API, la interfaz ni el motor (lo comprueba `tests/vendors/test_registry.py`).

    get_driver("hikvision")            → DriverSpec | None
    best_match(DetectionHints(...))    → [(DriverSpec, puntuación 0..1)] de mayor a menor
    preset_for("dahua", 3, "nvr")      → StreamPreset | None
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import re

from vms.core.interfaces import Capability, DetectionHints, DriverPublic, DriverSpec, StreamPreset
from vms.core.models import VENDOR_ID_PATTERN, DeviceKind, register_vendor_ids

log = logging.getLogger("vms.vendors.registry")

REGISTRY: dict[str, DriverSpec] = {}
MATURITY_ORDER = {"experimental": 0, "community": 1, "fixtures": 2, "verified": 3}
API_CAPABILITIES = frozenset({Capability.API_PROBE, Capability.API_CHANNELS, Capability.API_SNAPSHOT,
                              Capability.API_CODEC_FIX, Capability.TIME_READ, Capability.SECURITY_READ})
_ID_RE = re.compile(VENDOR_ID_PATTERN)


class RegistryError(ValueError):
    """Un driver mal declarado: falla al importar, no en producción con un equipo delante."""


def register(spec: DriverSpec) -> DriverSpec:
    if not _ID_RE.match(spec.id):
        raise RegistryError(f"id de driver no válido: {spec.id!r}")
    old = REGISTRY.get(spec.id)
    if old is not None and old is not spec:
        raise RegistryError(f"driver duplicado: {spec.id}")
    if spec.maturity not in MATURITY_ORDER:
        raise RegistryError(f"{spec.id}: madurez desconocida {spec.maturity!r}")
    if not spec.kinds:
        raise RegistryError(f"{spec.id}: declara al menos un tipo de equipo")
    if spec.client is None and spec.capabilities & (API_CAPABILITIES - {Capability.TIME_READ}) \
            and Capability.ONVIF not in spec.capabilities:
        raise RegistryError(f"{spec.id}: declara capacidades de API sin cliente")
    if Capability.API_CODEC_FIX in spec.capabilities and spec.client is None:
        raise RegistryError(f"{spec.id}: «Corregir códec» necesita cliente de API")
    if spec.presets is not None:
        for kind in spec.kinds:
            p = spec.presets(1, kind)
            for path in (p.main, p.sub or ""):
                if path and not path.startswith("/"):
                    raise RegistryError(f"{spec.id}: la ruta {path!r} tiene que empezar por «/»")
                if "//" in path and not spec.double_slash:
                    raise RegistryError(f"{spec.id}: ruta con «//» sin declararlo (double_slash=True)")
    REGISTRY[spec.id] = spec
    register_vendor_ids([spec.id])
    return spec


def get_driver(vendor_id: str) -> DriverSpec | None:
    return REGISTRY.get(vendor_id)


def drivers() -> list[DriverSpec]:
    """Todos los drivers: primero los de API, después perfiles por nombre y al final el RTSP manual."""
    def key(s: DriverSpec) -> tuple[int, str]:
        order = 0 if s.client is not None and s.id != "onvif" else 1 if s.id == "onvif" else \
            3 if s.id == "generic" else 2
        return order, s.name.lower()
    return sorted(REGISTRY.values(), key=key)


def public_vendors() -> list[DriverPublic]:
    return [s.public() for s in drivers()]


def best_match(hints: DetectionHints) -> list[tuple[DriverSpec, float]]:
    """Drivers con puntuación > 0, de mayor a menor. Un detector que falla no tumba la búsqueda."""
    scored: list[tuple[DriverSpec, float]] = []
    for spec in REGISTRY.values():
        try:
            score = float(spec.detect(hints))
        except Exception:  # noqa: BLE001 - un detector roto se registra y puntúa 0
            log.exception("El detector del driver %s falló", spec.id)
            continue
        if score > 0:
            scored.append((spec, max(0.0, min(1.0, score))))
    scored.sort(key=lambda t: (-t[1], t[0].id in ("onvif", "generic"), t[0].id))
    return scored


def guess_vendor_id(hints: DetectionHints, *, threshold: float = 0.5) -> tuple[str, float]:
    """(id, puntuación) de la marca más probable; «onvif» si nada pasa el umbral."""
    matches = best_match(hints)
    if matches and matches[0][1] >= threshold:
        return matches[0][0].id, matches[0][1]
    return "onvif", matches[0][1] if matches and matches[0][0].id == "onvif" else 0.0


def preset_for(vendor_id: str, channel: int, kind: DeviceKind = "camera") -> StreamPreset | None:
    channel = int(channel)
    if not 1 <= channel <= 512:
        raise ValueError("El canal debe estar entre 1 y 512")
    spec = REGISTRY.get(vendor_id)
    if spec is None or spec.presets is None:
        return None
    return spec.presets(channel, kind)


def variants_for(vendor_id: str, channel: int, kind: DeviceKind = "camera") -> tuple[StreamPreset, ...]:
    spec = REGISTRY.get(vendor_id)
    if spec is None or spec.path_variants is None:
        return ()
    return spec.path_variants(int(channel), kind)


def load_drivers() -> list[str]:
    """Importa cada `vms/vendors/drivers/<id>.py` (cada uno se registra solo)."""
    from . import drivers as pkg

    loaded: list[str] = []
    for mod in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{pkg.__name__}.{mod.name}")
        loaded.append(mod.name)
    return loaded


load_drivers()

__all__ = ["REGISTRY", "RegistryError", "best_match", "drivers", "get_driver", "guess_vendor_id", "load_drivers",
           "preset_for", "public_vendors", "register", "variants_for"]
