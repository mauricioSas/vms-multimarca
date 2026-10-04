"""Construye la lista de CameraSource (URLs RTSP con credenciales) a partir de la configuración."""
from __future__ import annotations

import logging

from . import rtsp
from .credentials import CredentialStore
from .interfaces import CameraSource
from .models import AppConfig, Camera, Device

log = logging.getLogger(__name__)


def camera_paths(device: Device, camera: Camera) -> tuple[str, str | None]:
    """(ruta principal, ruta subflujo o None). La ruta manual de la cámara manda sobre el preset."""
    preset = rtsp.preset_paths(device.vendor, camera.channel)
    main = camera.main_path or (preset[0] if preset else "")
    if not main:
        raise ValueError(f"La cámara «{camera.name}» no tiene ruta RTSP principal")
    sub: str | None = None
    if camera.has_sub:
        sub = camera.sub_path or (preset[1] if preset else None)
    return main, sub


def build_camera_sources(cfg: AppConfig, creds: CredentialStore) -> list[CameraSource]:
    """Solo cámaras habilitadas de equipos habilitados. Las que no tienen ruta se omiten con aviso."""
    out: list[CameraSource] = []
    passwords: dict[str, str] = {}
    for cam in cfg.cameras:
        dev = cfg.device(cam.device_id)
        if dev is None or not dev.enabled or not cam.enabled:
            continue
        try:
            main, sub = camera_paths(dev, cam)
        except ValueError as exc:
            log.warning("%s", exc)
            continue
        if dev.id not in passwords:
            passwords[dev.id] = creds.get_device_password(dev.id)
        pw = passwords[dev.id]
        out.append(CameraSource(
            camera_id=cam.id,
            name=cam.name,
            main_url=rtsp.build_rtsp_url(dev.host, dev.rtsp_port, main, dev.username, pw),
            sub_url=rtsp.build_rtsp_url(dev.host, dev.rtsp_port, sub, dev.username, pw) if sub else None,
            record=cam.record,
            rtsp_transport=cam.rtsp_transport,
        ))
    return out
