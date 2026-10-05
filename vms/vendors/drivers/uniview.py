"""Uniview (UNV): perfil RTSP + ONVIF. Prioridad P2 (PLAN-V2 §3.4). LAPI no tiene documentación pública."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec, LockoutPolicy

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, HINT_READ_ONLY_USER, NOTE_H265_SUB, fixed

SPEC = register(DriverSpec(
    id="uniview",
    name="Uniview",
    brands=("Uniview", "UNV"),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.ONVIF, Capability.DISCOVERY_WSD}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-md5",),
    maturity="community",
    detect=Detector(names=("UNIVIEW", "UNV"), model_regex=r"^(IPC\d{3,4}|NVR3\d{2}|HIC\d|IPC-?S\d{3})",
                    rivals=("DAHUA", "HIKVISION")),
    lockout=LockoutPolicy(attempts=5, minutes=10),
    presets=fixed("/unicast/c{ch}/s0/live", "/unicast/c{ch}/s1/live", "/unicast/c{ch}/s2/live"),
    notes_es=(
        "Si el puerto RTSP 554 no responde, prueba el 9090 (algunos modelos lo usan).",
        NOTE_H265_SUB,
    ),
    setup_hints_es=(HINT_READ_ONLY_USER, HINT_FIXED_IP),
))
