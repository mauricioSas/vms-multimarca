"""Bosch: perfil RTSP + ONVIF. Prioridad P3 (PLAN-V2 §3.4). RCP+ queda para después."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, fixed

SPEC = register(DriverSpec(
    id="bosch",
    name="Bosch",
    brands=("Bosch",),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.ONVIF, Capability.DISCOVERY_WSD}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-md5",),
    maturity="community",
    detect=Detector(names=("BOSCH",), model_regex=r"^(NBN|NDE|NDI|NBE|NDV|NDP|NTI|NUC|MIC|VIP|VJT|FLEXIDOME|"
                    r"DINION|AUTODOME)", rivals=("HIKVISION", "DAHUA")),
    presets=fixed("/?inst=1&line={ch}", "/?inst=2&line={ch}", query_safe=False),
    notes_es=("En codificadores de varias entradas, «line» es el número de entrada (canal).",),
    setup_hints_es=(HINT_FIXED_IP,),
))
