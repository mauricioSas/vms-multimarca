"""Hanwha Vision (Wisenet): perfil RTSP + ONVIF. Prioridad P2 (PLAN-V2 §3.4). SUNAPI queda para después."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec, StreamPreset
from vms.core.models import DeviceKind

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, NOTE_H265_SUB


def presets(channel: int, kind: DeviceKind) -> StreamPreset:
    """/profileN/media.smp; en codificadores y multisensor, /<canal-1>/profileN/media.smp."""
    prefix = "" if kind == "camera" and channel == 1 else f"/{channel - 1}"
    return StreamPreset(main=f"{prefix}/profile2/media.smp", sub=f"{prefix}/profile3/media.smp")


SPEC = register(DriverSpec(
    id="hanwha",
    name="Hanwha Vision (Wisenet)",
    brands=("Hanwha", "Wisenet", "Samsung Techwin"),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.ONVIF, Capability.ONVIF_MEDIA2, Capability.DISCOVERY_WSD}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-sha256", "digest-md5"),
    maturity="community",
    detect=Detector(names=("HANWHA", "WISENET", "SAMSUNG TECHWIN"), model_regex=r"^(XN[BDOPVF]|QN[BDOV]|PN[MOV]|"
                    r"LN[BOV]|AN[BDOV]|TN[BOVM]|SN[BDOP])-", rivals=("HIKVISION", "DAHUA")),
    presets=presets,
    notes_es=(
        "Los números de perfil dependen de la configuración de la cámara: por defecto profile2 es el principal "
        "(H.264) y profile3 el subflujo. Compruébalo en la web de la cámara o importa los canales por ONVIF.",
        NOTE_H265_SUB,
    ),
    setup_hints_es=(HINT_FIXED_IP,),
))
