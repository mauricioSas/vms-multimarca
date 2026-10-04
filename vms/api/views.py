"""Construcción de las respuestas DeviceOut y CameraOut (CONTRATO §6.3 y §6.4)."""
from __future__ import annotations

import logging
from typing import Any

from vms.core.credentials import CredentialError, CredentialStore
from vms.core.interfaces import PathStatus
from vms.core.models import AppConfig, Camera, Device

log = logging.getLogger("vms.api")

BROWSER_CODECS = ("H264",)


def has_password(creds: CredentialStore, device_id: str) -> bool:
    try:
        return creds.has_device_password(device_id)
    except CredentialError as exc:
        log.warning("No se pudo consultar el almacén de contraseñas: %s", exc)
        return False


def live_stream(cam: Camera) -> str:
    return "sub" if cam.has_sub else "main"


def codec_warning(cam: Camera, paths: dict[str, PathStatus] | None) -> str | None:
    if not paths:
        return None
    stream = live_stream(cam)
    st = paths.get(f"{cam.id}/{stream}")
    if st is None or not st.tracks:
        return None
    video = [t for t in st.tracks if t.upper() in ("H264", "H265", "MJPEG", "MPEG-4 VIDEO", "VP8", "VP9", "AV1")]
    if not video or any(t.upper() in BROWSER_CODECS for t in video):
        return None
    codec = video[0].upper().replace("H265", "H.265")
    flujo = "El subflujo" if stream == "sub" else "El flujo principal"
    return (f"{flujo} es {codec}: el navegador no lo reproduce por WebRTC. "
            "Cámbialo a H.264 en el NVR o en la cámara")


def camera_live(cam: Camera, device: Device | None, paths: dict[str, PathStatus] | None) -> dict[str, Any]:
    active = cam.enabled and device is not None and device.enabled
    if paths is None or not active:
        return {"online": None if active else False, "recording": False, "readers": 0, "tracks": [],
                "codec_warning": None, "last_error": ""}
    main = paths.get(f"{cam.id}/main")
    sub = paths.get(f"{cam.id}/sub")
    view = paths.get(f"{cam.id}/{live_stream(cam)}")
    return {
        "online": bool(main and main.ready),
        "recording": bool(main and main.recording),
        "readers": (main.readers if main else 0) + (sub.readers if sub else 0),
        "tracks": list((view.tracks if view and view.tracks else (main.tracks if main else [])) or []),
        "codec_warning": codec_warning(cam, paths),
        "last_error": main.last_error if main else "",
    }


def camera_out(cfg: AppConfig, cam: Camera, paths: dict[str, PathStatus] | None) -> dict[str, Any]:
    dev = cfg.device(cam.device_id)
    data = cam.model_dump(mode="json")
    data["device_name"] = dev.name if dev else ""
    data["vendor"] = dev.vendor if dev else "generic"
    data["live"] = camera_live(cam, dev, paths)
    return data


def device_out(cfg: AppConfig, dev: Device, creds: CredentialStore, paths: dict[str, PathStatus] | None,
               details: dict[str, Any] | None = None) -> dict[str, Any]:
    cams = cfg.cameras_of(dev.id)
    data = dev.model_dump(mode="json")
    data["has_password"] = has_password(creds, dev.id)
    data["cameras"] = [c.id for c in cams]
    online: bool | None = None
    if paths is not None and dev.enabled:
        active = [c for c in cams if c.enabled]
        if active:
            online = any((st := paths.get(f"{c.id}/main")) is not None and st.ready for c in active)
    data["online"] = online
    if details:
        data["details"] = details
    return data
