"""Hikvision: API ISAPI por HTTP con autenticación Digest (implementación propia).

Endpoints usados:
  GET /ISAPI/System/deviceInfo                          modelo, serie, firmware, tipo
  GET /ISAPI/ContentMgmt/InputProxy/channels            canales IP de un NVR (id, nombre, IP)
  GET /ISAPI/ContentMgmt/InputProxy/channels/status     en línea / fuera de línea por canal
  GET /ISAPI/Streaming/channels                         códec y resolución (101, 102, 201...)
  GET /ISAPI/Streaming/channels/<N>0<1|2>/picture       snapshot JPEG (solo en memoria)

Numeración: el canal N del NVR se corresponde con los flujos N01 (principal) y N02 (subflujo),
igual que las rutas RTSP /Streaming/Channels/N01. En grabadores híbridos (DVR/HVR con entradas
analógicas) los canales IP pueden empezar en un número mayor (p. ej. 33); se respeta el id
que devuelve el equipo.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Literal

import httpx

from vms.core.errors import DeviceProtocolError
from vms.core.interfaces import ChannelInfo, DeviceInfo
from vms.core.models import DeviceBase, Vendor

from ._http import VendorHttp

log = logging.getLogger("vms.vendors.hikvision")

NVR_TYPES = ("NVR", "DVR", "HVR", "XVR", "IPDVR", "DVS")


def _strip_ns(root: ET.Element) -> ET.Element:
    """Quita los espacios de nombres (ver10/ver20 varían entre firmwares)."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_xml(text: str, what: str) -> ET.Element:
    try:
        return _strip_ns(ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text))
    except ET.ParseError as exc:
        raise DeviceProtocolError(f"Respuesta XML no válida del equipo Hikvision ({what})") from exc


def _text(el: ET.Element | None, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.findtext(path)
    return found.strip() if found else default


def _codec(value: str) -> str | None:
    v = value.strip().upper().replace("H.", "H").replace("HEVC", "H265")
    if not v:
        return None
    if v.startswith("H264"):
        return "H.264"
    if v.startswith("H265"):
        return "H.265"
    return value.strip()


class HikvisionClient:
    vendor: Vendor = "hikvision"

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.device = device
        self.http = VendorHttp(device, password, timeout=timeout, transport=transport)
        self._info: DeviceInfo | None = None

    async def probe(self) -> DeviceInfo:
        resp = await self.http.get("/ISAPI/System/deviceInfo")
        root = parse_xml(resp.text, "deviceInfo")
        if root.tag != "DeviceInfo":
            raise DeviceProtocolError("El equipo respondió, pero no parece un Hikvision (ISAPI)")
        dtype = _text(root, "deviceType").upper()
        kind: Literal["camera", "nvr", "unknown"]
        if any(t in dtype for t in NVR_TYPES):
            kind = "nvr"
        elif dtype:
            kind = "camera"
        else:
            kind = "unknown"
        info = DeviceInfo(vendor="hikvision", kind=kind, model=_text(root, "model"),
                          serial=_text(root, "serialNumber"), firmware=_text(root, "firmwareVersion"),
                          mac=_text(root, "macAddress"), name=_text(root, "deviceName"))
        if kind == "nvr":
            try:
                info.channel_count = len(await self._proxy_channels())
            except DeviceProtocolError as exc:
                log.warning("No se pudo contar los canales de %s: %s", self.http.label, exc)
        else:
            info.channel_count = 1
        self._info = info
        return info

    async def _proxy_channels(self) -> list[tuple[int, str, str | None]]:
        resp = await self.http.get("/ISAPI/ContentMgmt/InputProxy/channels", ok_404=True)
        if resp.status_code == 404:
            return []
        root = parse_xml(resp.text, "InputProxy/channels")
        out: list[tuple[int, str, str | None]] = []
        for ch in root.iter("InputProxyChannel"):
            try:
                cid = int(_text(ch, "id"))
            except ValueError:
                continue
            ip = _text(ch, "sourceInputPortDescriptor/ipAddress") or None
            out.append((cid, _text(ch, "name"), ip))
        return out

    async def _proxy_status(self) -> dict[int, bool]:
        resp = await self.http.get("/ISAPI/ContentMgmt/InputProxy/channels/status", ok_404=True)
        if resp.status_code == 404:
            return {}
        root = parse_xml(resp.text, "InputProxy/channels/status")
        out: dict[int, bool] = {}
        for st in root.iter("InputProxyChannelStatus"):
            try:
                out[int(_text(st, "id"))] = _text(st, "online").lower() == "true"
            except ValueError:
                continue
        return out

    async def _streaming(self) -> dict[int, dict[str, str | None]]:
        """{id_flujo: {codec, resolution, name}} de /ISAPI/Streaming/channels."""
        resp = await self.http.get("/ISAPI/Streaming/channels", ok_404=True)
        if resp.status_code == 404:
            return {}
        root = parse_xml(resp.text, "Streaming/channels")
        out: dict[int, dict[str, str | None]] = {}
        for sc in root.iter("StreamingChannel"):
            try:
                sid = int(_text(sc, "id"))
            except ValueError:
                continue
            w, h = _text(sc, "Video/videoResolutionWidth"), _text(sc, "Video/videoResolutionHeight")
            out[sid] = {"codec": _codec(_text(sc, "Video/videoCodecType")),
                        "resolution": f"{w}x{h}" if w and h else None,
                        "name": _text(sc, "channelName") or None}
        return out

    async def list_channels(self) -> list[ChannelInfo]:
        info = self._info or await self.probe()
        streams = await self._streaming()
        channels: list[ChannelInfo] = []
        if info.kind == "nvr":
            proxies = await self._proxy_channels()
            status = await self._proxy_status() if proxies else {}
            for cid, name, ip in proxies:
                main, sub = streams.get(cid * 100 + 1, {}), streams.get(cid * 100 + 2)
                channels.append(ChannelInfo(
                    channel=cid, name=name or f"Canal {cid}", online=status.get(cid), has_sub=sub is not None or not streams,
                    main_codec=main.get("codec"), sub_codec=(sub or {}).get("codec"),
                    main_resolution=main.get("resolution"), sub_resolution=(sub or {}).get("resolution"),
                    ip_address=ip))
            if not proxies:  # NVR sin InputProxy (DVR analógico): se deduce de los flujos
                for cid in sorted({sid // 100 for sid in streams}):
                    main, sub = streams.get(cid * 100 + 1, {}), streams.get(cid * 100 + 2)
                    channels.append(ChannelInfo(
                        channel=cid, name=main.get("name") or f"Canal {cid}", has_sub=sub is not None,
                        main_codec=main.get("codec"), sub_codec=(sub or {}).get("codec"),
                        main_resolution=main.get("resolution"), sub_resolution=(sub or {}).get("resolution")))
        else:
            main, sub = streams.get(101, {}), streams.get(102)
            channels.append(ChannelInfo(
                channel=1, name=main.get("name") or info.name or self.device.name, online=True,
                has_sub=sub is not None or not streams,
                main_codec=main.get("codec"), sub_codec=(sub or {}).get("codec"),
                main_resolution=main.get("resolution"), sub_resolution=(sub or {}).get("resolution")))
        return channels

    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        sid = int(channel) * 100 + (1 if stream == "main" else 2)
        resp = await self.http.get(f"/ISAPI/Streaming/channels/{sid}/picture")
        if not resp.content.startswith(b"\xff\xd8"):
            raise DeviceProtocolError(f"El equipo {self.http.label} no devolvió una imagen JPEG")
        return resp.content

    async def aclose(self) -> None:
        await self.http.aclose()
