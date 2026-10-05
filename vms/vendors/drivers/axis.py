"""Axis: perfil RTSP + ONVIF. Prioridad P2 (PLAN-V2 §3.4). VAPIX queda para después."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, fixed

SPEC = register(DriverSpec(
    id="axis",
    name="Axis",
    brands=("Axis",),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.ONVIF, Capability.ONVIF_MEDIA2, Capability.DISCOVERY_WSD}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-sha256", "digest-md5"),
    maturity="community",
    detect=Detector(names=("AXIS",), model_regex=r"^(AXIS\s)?[MPQFV]\d{4}", rivals=("HIKVISION", "DAHUA")),
    presets=fixed("/axis-media/media.amp?camera={ch}&videocodec=h264",
                  "/axis-media/media.amp?camera={ch}&videocodec=h264&resolution=640x360", query_safe=False),
    notes_es=(
        "Axis aplica las opciones de la URL: el subflujo se pide en H.264 a 640x360 (sin perfil de stream "
        "propio).",
    ),
    setup_hints_es=("Crea un usuario «Visor» (Viewer) en la web de la cámara para el VMS.", HINT_FIXED_IP),
))
