"""RTSP manual: la ruta se escribe a mano (equipos sin preset ni ONVIF)."""
from __future__ import annotations

from vms.core.interfaces import DriverSpec

from ..detect import constant
from ..registry import register

SPEC = register(DriverSpec(
    id="generic",
    name="Genérico (RTSP manual)",
    brands=("Genérico",),
    kinds=("camera", "nvr"),
    capabilities=frozenset(),
    default_ports={"rtsp": 554},
    auth=("digest-sha256", "digest-md5", "basic"),
    maturity="community",
    detect=constant(0.0),
    presets=None,
    client=None,
    notes_es=("Escribe la ruta RTSP tal como va después del puerto (por ejemplo /stream1), sin rtsp:// ni IP.",),
))
