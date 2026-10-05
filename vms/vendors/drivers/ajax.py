"""Ajax (cámaras de alarma): RTSP en el puerto 8554 con la ruta copiada de la app, y ONVIF. P2 (PLAN-V2 §3.4)."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP

SPEC = register(DriverSpec(
    id="ajax",
    name="Ajax",
    brands=("Ajax",),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.ONVIF, Capability.DISCOVERY_WSD}),
    default_ports={"http": 80, "rtsp": 8554, "onvif": 80},
    auth=("digest-md5",),
    maturity="experimental",
    detect=Detector(names=("AJAX",), rivals=("HIKVISION", "DAHUA")),
    presets=None,      # la ruta se copia de la app de Ajax (no hay una fija documentada)
    notes_es=(
        "El RTSP de Ajax va en el puerto 8554, no en el 554.",
        "Copia la ruta de vídeo desde la app de Ajax (Ajustes de la cámara > Enlaces RTSP) y pégala aquí sin "
        "rtsp://, IP ni usuario; o importa los canales por ONVIF.",
    ),
    setup_hints_es=("Activa ONVIF/RTSP en la app de Ajax para esa cámara o grabador.", HINT_FIXED_IP),
))
