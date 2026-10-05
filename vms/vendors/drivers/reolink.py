"""Reolink: perfil RTSP. Prioridad P3 (PLAN-V2 §3.4). API api.cgi y HTTP-FLV quedan para después."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, NOTE_H265_SUB, fixed

SPEC = register(DriverSpec(
    id="reolink",
    name="Reolink",
    brands=("Reolink",),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.DISCOVERY_WSD}),
    default_ports={"http": 80, "rtsp": 554},
    auth=("digest-md5",),
    maturity="community",
    detect=Detector(names=("REOLINK",), model_regex=r"^(RLC|RLN|E1|CX\d|DUO|TRACKMIX|RLK)", rivals=("HIKVISION",)),
    presets=fixed("/Preview_{ch2}_main", "/Preview_{ch2}_sub"),
    notes_es=(
        "En los firmwares nuevos RTSP, ONVIF y HTTP vienen desactivados de fábrica.",
        "Su RTSP es menos estable que el de otras marcas: si se corta a menudo, usa el subflujo para la vista.",
        NOTE_H265_SUB,
    ),
    setup_hints_es=(
        "Activa RTSP en la app o la web de Reolink (Ajustes > Red > Avanzado > Ajustes de puertos).",
        HINT_FIXED_IP,
    ),
))
