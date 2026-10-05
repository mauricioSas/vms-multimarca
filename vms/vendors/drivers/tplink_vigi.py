"""TP-Link VIGI: perfil RTSP + ONVIF. Prioridad P2 (PLAN-V2 §3.4)."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, NOTE_H265_SUB, fixed

SPEC = register(DriverSpec(
    id="tplink-vigi",
    name="TP-Link VIGI",
    brands=("TP-Link VIGI",),
    kinds=("camera",),
    capabilities=frozenset({Capability.ONVIF, Capability.DISCOVERY_WSD}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-md5",),
    maturity="community",
    detect=Detector(names=("VIGI",), model_regex=r"^VIGI\s?[CN]\d", rivals=("TAPO",)),
    presets=fixed("/stream1", "/stream2"),
    notes_es=(
        "Desactiva «Smart Coding» y usa H.264: con él activado la grabación se corrompe al reproducirla.",
        "En firmwares antiguos ONVIF va en el puerto 2020 en lugar del 80.",
        NOTE_H265_SUB,
    ),
    setup_hints_es=(HINT_FIXED_IP,),
))
