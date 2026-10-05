"""Hikvision (+HiLook, HiWatch, LTS, Annke): API ISAPI. Prioridad P1 (PLAN-V2 §3.4)."""
from __future__ import annotations

from vms.core.interfaces import Capability, DriverSpec, LockoutPolicy

from ..detect import Detector
from ..hikvision import HikvisionClient
from ..registry import register
from ._common import HINT_FIXED_IP, HINT_READ_ONLY_USER, NOTE_H265_SUB, hikvision_style

SPEC = register(DriverSpec(
    id="hikvision",
    name="Hikvision",
    brands=("Hikvision", "HiLook", "HiWatch", "LTS", "Annke"),
    kinds=("camera", "nvr", "dvr"),
    capabilities=frozenset({Capability.API_PROBE, Capability.API_CHANNELS, Capability.API_SNAPSHOT,
                            Capability.API_CODEC_FIX, Capability.ONVIF, Capability.DISCOVERY_WSD,
                            Capability.DISCOVERY_SADP, Capability.TIME_READ, Capability.SECURITY_READ}),
    default_ports={"http": 80, "https": 443, "rtsp": 554, "onvif": 80, "sdk": 8000},
    auth=("digest-sha256", "digest-md5", "basic"),
    maturity="community",
    detect=Detector(
        names=("HIKVISION", "HILOOK", "HIWATCH", "ANNKE"),
        model_regex=r"^(I?DS-\d|DS-[0-9A-Z]|HWI-|HWN-|HWD-|HWP-|HWK-)",
        weak_model_regex=r"^IPC-[BTD]\d",
        http_servers=("App-webs/", "DNVRS-Webs", "DVRDVS-Webs", "Hikvision-Webs"),
        # OUI orientativos (MAC de Hikvision vistas en equipos; hay OEM con otras): solo desempatan
        ouis=("c4:2f:90", "44:19:b6", "bc:ad:28", "4c:bd:8f", "54:c4:15", "28:57:be", "a4:14:37", "18:68:cb",
              "c0:56:e3", "80:7c:62"),
        sadp=True,
        rivals=("EZVIZ", "DAHUA", "UNIVIEW", "UNV", "AXIS", "HANWHA", "IMOU"),
        exclude_model_regex=r"^CS-"),
    lockout=LockoutPolicy(attempts=5, minutes=30),
    presets=hikvision_style,
    client=HikvisionClient,
    notes_es=(
        "Desde el firmware 5.5 ONVIF viene desactivado y usa usuarios propios: el alta usa la API ISAPI.",
        "H.264+/H.265+ alargan el GOP y el vídeo tarda más en arrancar; «Corregir códec» los quita del subflujo.",
        NOTE_H265_SUB,
        "En un DVR/HVR híbrido los canales IP pueden empezar en el 33: se usa el número que da el equipo.",
        "Tras 5 intentos fallidos el equipo bloquea el usuario 30 minutos: el alta prueba la contraseña una vez.",
    ),
    setup_hints_es=(
        HINT_READ_ONLY_USER,
        "Comprueba en Configuración > Red > Avanzado que RTSP esté activado en el puerto 554 (algunos firmwares "
        "lo desactivan al actualizarse).",
        HINT_FIXED_IP,
    ),
))
