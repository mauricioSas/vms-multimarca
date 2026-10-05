"""Imou (marca de consumo de Dahua): perfil RTSP con la ruta de Dahua. Prioridad P1 (PLAN-V2 §3.4)."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec, LockoutPolicy

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, NOTE_H265_SUB, dahua_style

SPEC = register(DriverSpec(
    id="imou",
    name="Imou",
    brands=("Imou",),
    kinds=("camera",),
    capabilities=frozenset({Capability.ONVIF, Capability.DISCOVERY_WSD, Capability.DISCOVERY_DHIP,
                            Capability.TIME_READ, Capability.SECURITY_READ}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-md5",),
    maturity="experimental",
    detect=Detector(names=("IMOU",), weak_model_regex=r"^IPC-[ACFGKS]\d", rivals=("DAHUA",)),
    lockout=LockoutPolicy(attempts=5, minutes=30),
    presets=dahua_style,
    notes_es=(
        "Usuario «admin» y, como contraseña, el «safety code» de la etiqueta de la cámara (no verificado con un "
        "equipo).",
        NOTE_H265_SUB,
    ),
    setup_hints_es=("La cámara tiene que estar dada de alta en la app Imou y en la misma red.", HINT_FIXED_IP),
))
