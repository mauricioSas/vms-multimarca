"""TP-Link Tapo: perfil RTSP + ONVIF (puerto 2020). Prioridad P2 (PLAN-V2 §3.4)."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, fixed

SPEC = register(DriverSpec(
    id="tapo",
    name="Tapo",
    brands=("Tapo",),
    kinds=("camera",),
    capabilities=frozenset({Capability.ONVIF, Capability.DISCOVERY_WSD, Capability.TIME_READ,
                            Capability.SECURITY_READ}),
    default_ports={"rtsp": 554, "onvif": 2020},
    auth=("digest-md5",),
    maturity="community",
    detect=Detector(names=("TAPO",), weak_model_regex=r"^(TAPO\s*)?C\d{3}\b", rivals=("VIGI",)),
    presets=fixed("/stream1", "/stream2"),
    notes_es=(
        "Las cámaras Tapo de batería no tienen RTSP: no se pueden grabar con el VMS.",
        "ONVIF va en el puerto 2020.",
    ),
    setup_hints_es=(
        "Crea una «cuenta de la cámara» en la app Tapo (Ajustes avanzados > Cuenta de la cámara) y usa ese usuario "
        "y contraseña aquí (no los de tu cuenta TP-Link).",
        HINT_FIXED_IP,
    ),
))
