"""ONVIF genérico con onvif-zeep-async (MIT): información, perfiles, URIs RTSP y snapshot.

Canales: cada «video source» del equipo es un canal (una cámara = 1; un NVR ONVIF = N).
Dentro de cada canal, el perfil de mayor resolución es el principal y el siguiente, el
subflujo. Las rutas RTSP descubiertas (parte tras host:puerto, con su query) se devuelven en
ChannelInfo.main_path/sub_path para guardarlas en la cámara (las URIs nunca llevan credenciales).
"""
from __future__ import annotations

import asyncio
import re
import logging
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit

from vms.core.errors import (DeviceAuthFailed, DeviceError, DeviceProtocolError, DeviceUnreachable,
                             DeviceUnsupported)
from vms.core.interfaces import ChannelInfo, DeviceInfo
from vms.core.models import DeviceBase, Vendor

from ._http import VendorHttp, device_label

log = logging.getLogger("vms.vendors.onvif")

T = TypeVar("T")

_AUTH_MARKERS = ("notauthorized", "not authorized", "unauthorized", "authority", "authentication")
# «401» solo como código suelto: no dentro de un puerto o una IP (p. ej. «10.0.0.2:4010»).
_AUTH_401_RE = re.compile(r"(?<![\d.:])401(?!\d)")


def rtsp_path_of(uri: str) -> str:
    """«rtsp://u:p@10.0.0.2:554/Streaming/Channels/101?x=1» → «/Streaming/Channels/101?x=1»."""
    parts = urlsplit(uri)
    path = parts.path or "/"
    return path + (f"?{parts.query}" if parts.query else "")


def _translate(exc: BaseException, label: str) -> DeviceError:
    if isinstance(exc, DeviceError):
        return exc
    text = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in text:
        return DeviceUnreachable(f"El equipo {label} no responde por ONVIF (tiempo agotado)")
    if any(m in text for m in _AUTH_MARKERS) or _AUTH_401_RE.search(text):
        return DeviceAuthFailed(f"Usuario o contraseña ONVIF incorrectos en {label}. Comprueba también que "
                                "el usuario ONVIF esté creado en el equipo y que su hora sea correcta.")
    if isinstance(exc, (OSError, ConnectionError)) or "cannot connect" in text or "connect" in text:
        return DeviceUnreachable(f"No se puede conectar por ONVIF con {label}: revisa la IP y el puerto ONVIF")
    return DeviceProtocolError(f"Error ONVIF en {label}: {type(exc).__name__}")


class OnvifClient:
    vendor: Vendor = "onvif"

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0) -> None:
        self.device = device
        self.password = password
        self.timeout = timeout
        self.port = device.onvif_port or device.http_port
        self.label = device_label(device)
        self._cam: Any = None
        self._media: Any = None
        self._profiles: list[Any] | None = None
        self._info: DeviceInfo | None = None

    async def _call(self, coro: Any) -> Any:
        try:
            return await asyncio.wait_for(coro, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - se traduce a error de dominio
            raise _translate(exc, self.label) from exc

    async def _camera(self) -> Any:
        if self._cam is None:
            try:
                from onvif import ONVIFCamera
            except ImportError as exc:  # pragma: no cover - dependencia del extra [vms]
                raise DeviceUnsupported("Falta el módulo ONVIF (onvif-zeep-async)") from exc
            cam = ONVIFCamera(self.device.host, self.port, self.device.username or None, self.password or None,
                              adjust_time=True)
            self._cam = cam
            await self._call(cam.update_xaddrs())
        return self._cam

    async def _media_service(self) -> Any:
        if self._media is None:
            cam = await self._camera()
            self._media = await self._call(cam.create_media_service())
        return self._media

    async def probe(self) -> DeviceInfo:
        cam = await self._camera()
        dm = await self._call(cam.create_devicemgmt_service())
        info_raw = await self._call(dm.GetDeviceInformation())
        manufacturer = str(getattr(info_raw, "Manufacturer", "") or "")
        sources = await self._grouped_profiles()
        info = DeviceInfo(
            vendor="onvif", kind="nvr" if len(sources) > 1 else "camera",
            model=str(getattr(info_raw, "Model", "") or ""), serial=str(getattr(info_raw, "SerialNumber", "") or ""),
            firmware=str(getattr(info_raw, "FirmwareVersion", "") or ""), name=manufacturer,
            channel_count=max(len(sources), 1))
        self._info = info
        return info

    async def _get_profiles(self) -> list[Any]:
        if self._profiles is None:
            media = await self._media_service()
            self._profiles = list(await self._call(media.GetProfiles()) or [])
        return self._profiles

    async def _grouped_profiles(self) -> list[list[Any]]:
        """Perfiles agrupados por video source, cada grupo ordenado de mayor a menor resolución."""
        groups: dict[str, list[Any]] = {}
        for p in await self._get_profiles():
            vsc = getattr(p, "VideoSourceConfiguration", None)
            if getattr(p, "VideoEncoderConfiguration", None) is None:
                continue  # perfiles solo de audio/metadatos
            key = str(getattr(vsc, "SourceToken", "") or "default") if vsc is not None else "default"
            groups.setdefault(key, []).append(p)

        def pixels(p: Any) -> int:
            res = getattr(getattr(p, "VideoEncoderConfiguration", None), "Resolution", None)
            return int(getattr(res, "Width", 0) or 0) * int(getattr(res, "Height", 0) or 0)

        return [sorted(g, key=pixels, reverse=True) for g in groups.values()]

    async def _stream_uri(self, token: str) -> str:
        media = await self._media_service()
        req = {"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}, "ProfileToken": token}
        res = await self._call(media.GetStreamUri(req))
        uri = str(getattr(res, "Uri", "") or "")
        if not uri.lower().startswith("rtsp"):
            raise DeviceProtocolError(f"{self.label} devolvió una URI de vídeo no RTSP")
        return uri

    @staticmethod
    def _describe(p: Any) -> tuple[str | None, str | None]:
        vec = getattr(p, "VideoEncoderConfiguration", None)
        enc = str(getattr(vec, "Encoding", "") or "").upper()
        codec = {"H264": "H.264", "H265": "H.265", "JPEG": "MJPEG"}.get(enc, enc or None)
        res = getattr(vec, "Resolution", None)
        w, h = getattr(res, "Width", None), getattr(res, "Height", None)
        return codec, (f"{w}x{h}" if w and h else None)

    async def list_channels(self) -> list[ChannelInfo]:
        groups = await self._grouped_profiles()
        if not groups:
            raise DeviceProtocolError(f"{self.label} no tiene perfiles de vídeo ONVIF")
        out: list[ChannelInfo] = []
        for i, group in enumerate(groups, start=1):
            main_p = group[0]
            sub_p = group[1] if len(group) > 1 else None
            main_uri = await self._stream_uri(main_p.token)
            sub_uri = await self._stream_uri(sub_p.token) if sub_p is not None else None
            main_port = urlsplit(main_uri).port
            if main_port and main_port != self.device.rtsp_port:
                log.warning("%s anuncia RTSP en el puerto %s pero el equipo está configurado con %s",
                            self.label, main_port, self.device.rtsp_port)
            main_codec, main_res = self._describe(main_p)
            sub_codec, sub_res = self._describe(sub_p) if sub_p is not None else (None, None)
            name = str(getattr(main_p, "Name", "") or "")
            out.append(ChannelInfo(
                channel=i, name=(f"Canal {i}" if len(groups) > 1 or not name else name), online=True,
                has_sub=sub_p is not None, main_codec=main_codec, sub_codec=sub_codec,
                main_resolution=main_res, sub_resolution=sub_res,
                main_path=rtsp_path_of(main_uri), sub_path=rtsp_path_of(sub_uri) if sub_uri else None))
        return out

    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        groups = await self._grouped_profiles()
        if not 1 <= channel <= len(groups):
            raise DeviceProtocolError(f"El canal {channel} no existe en {self.label}")
        group = groups[channel - 1]
        profile = group[1] if stream == "sub" and len(group) > 1 else group[0]
        media = await self._media_service()
        res = await self._call(media.GetSnapshotUri({"ProfileToken": profile.token}))
        uri = str(getattr(res, "Uri", "") or "")
        if not uri.lower().startswith("http"):
            raise DeviceUnsupported(f"{self.label} no ofrece snapshot por ONVIF")
        parts = urlsplit(uri)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        snap_dev = self.device.model_copy(update={"host": parts.hostname or self.device.host,
                                                  "https": parts.scheme == "https"})
        http = VendorHttp(snap_dev, self.password, timeout=self.timeout,
                          port=parts.port or (443 if parts.scheme == "https" else 80))
        try:
            resp = await http.get(path)
        finally:
            await http.aclose()
        if not resp.content.startswith(b"\xff\xd8"):
            raise DeviceProtocolError(f"El equipo {self.label} no devolvió una imagen JPEG")
        return resp.content

    async def aclose(self) -> None:
        if self._cam is not None:
            try:
                await self._cam.close()
            except Exception:  # noqa: BLE001 - cerrar nunca debe romper la petición
                log.debug("Error al cerrar la sesión ONVIF de %s", self.label, exc_info=True)
            self._cam = None

