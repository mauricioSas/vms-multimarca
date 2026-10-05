"""Ezviz (marca de consumo de Hikvision): perfil RTSP. Prioridad P1 (PLAN-V2 §3.4)."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec, LockoutPolicy, StreamPreset
from vms.core.models import DeviceKind

from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, NOTE_H265_SUB, fixed


def variants(channel: int, kind: DeviceKind) -> tuple[StreamPreset, ...]:
    """Rutas que también usan los firmwares de Ezviz (solo si la principal da 404 con la contraseña aceptada)."""
    return (StreamPreset(main="/h264/ch1/main/av_stream", sub="/h264/ch1/sub/av_stream"),
            StreamPreset(main="/Streaming/Channels/101", sub="/Streaming/Channels/102"))


SPEC = register(DriverSpec(
    id="ezviz",
    name="Ezviz",
    brands=("Ezviz",),
    kinds=("camera",),
    capabilities=frozenset({Capability.DISCOVERY_SADP}),
    default_ports={"rtsp": 554},
    auth=("digest-md5",),
    maturity="community",
    detect=Detector(names=("EZVIZ",), model_regex=r"^CS-", rivals=("HIKVISION",)),
    lockout=LockoutPolicy(attempts=5, minutes=30),
    presets=fixed("/ch1/main", "/ch1/sub"),
    path_variants=variants,
    notes_es=(
        "Usuario «admin» y, como contraseña, el código de verificación de la etiqueta de la cámara (6 letras).",
        "Con el cifrado de vídeo activo, la contraseña RTSP es la del cifrado (no verificado con un equipo).",
        NOTE_H265_SUB,
    ),
    setup_hints_es=(
        "Activa RTSP en la app Ezviz (Ajustes de la cámara > Vista en directo LAN / RTSP); viene apagado de fábrica.",
        HINT_FIXED_IP,
    ),
))
