"""Dahua (+Amcrest, Lorex): API CGI. Prioridad P1 (PLAN-V2 §3.4)."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec, LockoutPolicy

from ..dahua import DahuaClient
from ..detect import Detector
from ..registry import register
from ._common import HINT_FIXED_IP, HINT_READ_ONLY_USER, NOTE_H265_SUB, dahua_style

SPEC = register(DriverSpec(
    id="dahua",
    name="Dahua",
    brands=("Dahua", "Amcrest", "Lorex"),
    kinds=("camera", "nvr", "dvr", "xvr"),
    capabilities=frozenset({Capability.API_PROBE, Capability.API_CHANNELS, Capability.API_SNAPSHOT,
                            Capability.API_CODEC_FIX, Capability.ONVIF, Capability.DISCOVERY_WSD,
                            Capability.DISCOVERY_DHIP, Capability.TIME_READ, Capability.SECURITY_READ}),
    default_ports={"http": 80, "https": 443, "rtsp": 554, "onvif": 80, "sdk": 37777},
    auth=("digest-md5", "basic"),
    maturity="community",
    detect=Detector(
        names=("DAHUA", "AMCREST", "LOREX"),
        model_regex=r"^(DHI?-|IPC-H[A-Z]{1,3}\d|IPC-EB|IPC-PF|SD\d{2}|NVR[245]\d{3}|XVR\d|HCVR\d|IVSS|DH-)",
        weak_model_regex=r"^IPC-[A-Z]",
        ouis=("3c:ef:8c", "90:02:a9", "e0:50:8b", "4c:11:bf", "14:a7:8b", "38:af:29", "9c:14:63", "a0:bd:1d",
              "bc:32:5f"),
        dhip=True,
        rivals=("IMOU", "HIKVISION", "UNIVIEW", "UNV", "EZVIZ"),
        exclude_model_regex=r"^IPC-[ACFGKS]\d"),
    lockout=LockoutPolicy(attempts=5, minutes=30),
    presets=dahua_style,
    client=DahuaClient,
    notes_es=(
        NOTE_H265_SUB,
        "En un XVR los canales IP van detrás de los analógicos (p. ej. 4 analógicos y las IP desde el 5): se usa "
        "el número que da el equipo.",
        "Tras 5 intentos fallidos el equipo bloquea el usuario 30 minutos: el alta prueba la contraseña una vez.",
    ),
    setup_hints_es=(HINT_READ_ONLY_USER, HINT_FIXED_IP),
))
