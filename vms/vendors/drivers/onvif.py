"""ONVIF genérico (Profile S y T): cualquier marca que no tenga driver propio."""
from __future__ import annotations

from vms.core.interfaces import Capability, DetectionHints, DriverSpec

from ..onvif_client import OnvifClient
from ..registry import register
from ._common import HINT_FIXED_IP


def detect(hints: DetectionHints) -> float:
    # Respondió a WS-Discovery como transmisor de vídeo ONVIF: seguro que habla ONVIF, aunque otra marca puntúe más
    return 0.4 if hints.scopes else 0.05


SPEC = register(DriverSpec(
    id="onvif",
    name="ONVIF (otras marcas)",
    brands=("ONVIF",),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.API_PROBE, Capability.API_CHANNELS, Capability.API_SNAPSHOT,
                            Capability.ONVIF, Capability.ONVIF_MEDIA2, Capability.DISCOVERY_WSD,
                            Capability.TIME_READ, Capability.SECURITY_READ}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-sha256", "digest-md5"),
    maturity="community",
    detect=detect,
    presets=None,
    client=OnvifClient,
    notes_es=(
        "Las rutas de vídeo se piden al equipo (GetStreamUri). Con H.265 se usa Media2 (Profile T) si el equipo "
        "lo tiene.",
        "Si la contraseña es buena pero ONVIF la rechaza, revisa la hora del equipo y que el usuario ONVIF exista "
        "(en algunas marcas es distinto del de la web).",
    ),
    setup_hints_es=("Activa ONVIF en el equipo y crea un usuario ONVIF si la marca lo pide.", HINT_FIXED_IP),
))
