"""Milesight: perfil RTSP + ONVIF. Prioridad P2 (PLAN-V2 §3.4).

Rutas `/main`, `/sub` y `/third`; hay fuentes que piden doble barra (`//main`): se prueban solo si la ruta
normal da 404 con la contraseña ya aceptada (nunca tras un 401).
"""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec, StreamPreset
from vms.core.models import DeviceKind

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, NOTE_H265_SUB


def presets(channel: int, kind: DeviceKind) -> StreamPreset:
    # Multisensor y NVR: /sensorN/… o /chN/… según el modelo (sin verificar); cámara: /main y /sub
    if channel == 1:
        return StreamPreset(main="/main", sub="/sub", third="/third")
    return StreamPreset(main=f"/sensor{channel}/main", sub=f"/sensor{channel}/sub")


def variants(channel: int, kind: DeviceKind) -> tuple[StreamPreset, ...]:
    if channel == 1:
        return (StreamPreset(main="//main", sub="//sub"),)
    return ()


SPEC = register(DriverSpec(
    id="milesight",
    name="Milesight",
    brands=("Milesight",),
    kinds=("camera", "nvr"),
    capabilities=frozenset({Capability.ONVIF, Capability.DISCOVERY_WSD, Capability.TIME_READ,
                            Capability.SECURITY_READ}),
    default_ports={"http": 80, "rtsp": 554, "onvif": 80},
    auth=("digest-md5",),
    maturity="community",
    detect=Detector(names=("MILESIGHT",), model_regex=r"^MS-C\d{4}", rivals=("HIKVISION", "DAHUA")),
    presets=presets,
    path_variants=variants,
    double_slash=True,
    notes_es=(
        "Algunos firmwares usan la ruta con doble barra (//main): el alta la prueba sola si /main da 404.",
        NOTE_H265_SUB,
    ),
    setup_hints_es=(HINT_FIXED_IP,),
))
