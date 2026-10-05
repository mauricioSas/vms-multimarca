"""Hikvision: API ISAPI por HTTP con autenticación Digest (implementación propia).

Endpoints usados (todos de lectura salvo «Corregir códec»):
  GET /ISAPI/System/deviceInfo                          modelo, serie, firmware y fecha, MAC, tipo
  GET /ISAPI/ContentMgmt/InputProxy/channels[/status]   canales IP de un NVR/DVR (id, nombre, IP, en línea)
  GET /ISAPI/System/Video/inputs/channels               entradas analógicas de un DVR/HVR híbrido
  GET /ISAPI/Streaming/channels                         códec, resolución, tasa, GOP y Smart Codec por flujo
  GET /ISAPI/Streaming/channels/<N>0<1|2>/picture       snapshot JPEG (solo en memoria)
  GET/PUT /ISAPI/Streaming/channels/<N>02               «Corregir códec» (copia previa y deshacer)
  GET /ISAPI/System/time, /ISAPI/System/time/ntpServers hora y modo (TIME_READ)
  GET /ISAPI/System/Network/{telnetd,ssh,UPnP,EZVIZ}, /ISAPI/Security/adminAccesses   (SECURITY_READ)

Numeración: el canal N corresponde a los flujos N01 (principal) y N02 (subflujo), igual que las rutas RTSP
/Streaming/Channels/N01. En grabadores híbridos (DVR/HVR) los canales IP pueden empezar en un número mayor
(p. ej. 33): **siempre se usa el id que devuelve el equipo** (PLAN-V2 §3.2 punto 6; no verificado con hardware).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timezone
from typing import Literal

import httpx

from vms.core.errors import DeviceError, DeviceProtocolError
from vms.core.interfaces import ChannelInfo, DeviceInfo, DeviceSecuritySettings, DeviceTime
from vms.core.models import DeviceBase, Vendor

from ._http import VendorHttp
from .clock import Stopwatch, parse_device_datetime, posix_offset
from .codec import StreamConfigBackup, normalize_codec

log = logging.getLogger("vms.vendors.hikvision")

NVR_TYPES = ("NVR", "IPDVR", "DVS")
DVR_TYPES = ("DVR", "HVR", "XVR", "HYBIRD", "HYBRID")
_BUILD_RE = re.compile(r"(?:build\s*)?(\d{2})(\d{2})(\d{2})\b", re.IGNORECASE)


def _strip_ns(root: ET.Element) -> ET.Element:
    """Quita los espacios de nombres (ver10/ver20 varían entre firmwares)."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_xml(text: str | bytes, what: str) -> ET.Element:
    try:
        return _strip_ns(ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text))
    except ET.ParseError as exc:
        raise DeviceProtocolError(f"Respuesta XML no válida del equipo Hikvision ({what})") from exc


def _text(el: ET.Element | None, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.findtext(path)
    return found.strip() if found else default


def _int(value: str) -> int | None:
    return int(value) if value.strip().lstrip("-").isdigit() else None


def build_date(text: str) -> str:
    """«build 210812» → «2021-08-12» (la auditoría compara Hikvision por fecha de build)."""
    m = _BUILD_RE.search(text or "")
    if not m:
        return ""
    yy, mm, dd = m.groups()
    if not (1 <= int(mm) <= 12 and 1 <= int(dd) <= 31):
        return ""
    return f"20{yy}-{mm}-{dd}"


@dataclass
class _Stream:
    codec: str | None = None
    resolution: str | None = None
    name: str | None = None
    kbps: int | None = None
    gop_s: float | None = None
    smart: bool | None = None


def _bool(el: ET.Element | None, path: str) -> bool | None:
    v = _text(el, path).lower()
    return True if v == "true" else False if v == "false" else None


class HikvisionClient:
    vendor: Vendor = "hikvision"

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.device = device
        self._password = password
        self._timeout = timeout
        self._transport = transport
        self.http = VendorHttp(device, password, timeout=timeout, transport=transport)
        self._info: DeviceInfo | None = None
        if device.vendor and device.vendor != "hikvision":
            self.vendor = device.vendor   # marcas OEM que comparten ISAPI

    # ------------------------------------------------------------------ información
    async def probe(self) -> DeviceInfo:
        resp = await self.http.get("/ISAPI/System/deviceInfo")
        root = parse_xml(resp.text, "deviceInfo")
        if root.tag != "DeviceInfo":
            raise DeviceProtocolError("El equipo respondió, pero no parece un Hikvision (ISAPI)")
        dtype = _text(root, "deviceType").upper()
        kind: Literal["camera", "nvr", "dvr", "unknown"]
        if any(t in dtype for t in DVR_TYPES):
            kind = "dvr"
        elif any(t in dtype for t in NVR_TYPES):
            kind = "nvr"
        elif dtype:
            kind = "camera"
        else:
            kind = "unknown"
        info = DeviceInfo(vendor=self.vendor, kind=kind, model=_text(root, "model"),
                          serial=_text(root, "serialNumber"), firmware=_text(root, "firmwareVersion"),
                          firmware_date=build_date(_text(root, "firmwareReleasedDate")),
                          mac=_text(root, "macAddress").lower(), name=_text(root, "deviceName"))
        if kind in ("nvr", "dvr"):
            try:
                info.channel_count = len(await self._proxy_channels()) + len(await self._analog_inputs())
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
            cid = _int(_text(ch, "id"))
            if cid is None or cid < 1:
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
            cid = _int(_text(st, "id"))
            if cid is not None:
                out[cid] = _text(st, "online").lower() == "true"
        return out

    async def _analog_inputs(self) -> list[tuple[int, str, bool | None]]:
        """Entradas analógicas de un DVR/HVR (id, nombre, hay vídeo). Una cámara IP no las tiene."""
        if self._info is not None and self._info.kind == "camera":
            return []
        resp = await self.http.get("/ISAPI/System/Video/inputs/channels", ok_404=True)
        if resp.status_code == 404:
            return []
        root = parse_xml(resp.text, "Video/inputs/channels")
        out: list[tuple[int, str, bool | None]] = []
        for ch in root.iter("VideoInputChannel"):
            cid = _int(_text(ch, "id"))
            if cid is None or cid < 1 or _text(ch, "videoInputEnabled").lower() == "false":
                continue
            res = _text(ch, "resDesc").upper()
            online = False if res == "NO VIDEO" else (True if res else None)
            out.append((cid, _text(ch, "name"), online))
        return out

    async def _streaming(self) -> dict[int, _Stream]:
        """{id_flujo: _Stream} de /ISAPI/Streaming/channels (códec, resolución, tasa, GOP y Smart Codec)."""
        resp = await self.http.get("/ISAPI/Streaming/channels", ok_404=True)
        if resp.status_code == 404:
            return {}
        root = parse_xml(resp.text, "Streaming/channels")
        out: dict[int, _Stream] = {}
        for sc in root.iter("StreamingChannel"):
            sid = _int(_text(sc, "id"))
            if sid is None:
                continue
            w, h = _text(sc, "Video/videoResolutionWidth"), _text(sc, "Video/videoResolutionHeight")
            qc = _text(sc, "Video/videoQualityControlType").upper()
            kbps = _int(_text(sc, "Video/constantBitRate")) if qc == "CBR" else None
            kbps = kbps or _int(_text(sc, "Video/vbrUpperCap")) or _int(_text(sc, "Video/constantBitRate"))
            gov = _int(_text(sc, "Video/GovLength"))
            fps100 = _int(_text(sc, "Video/maxFrameRate"))
            gop_s = round(gov / (fps100 / 100), 2) if gov and fps100 else None
            smart = _bool(sc.find("Video"), "SmartCodec/enabled")
            codec_raw = _text(sc, "Video/videoCodecType")
            if "+" in codec_raw:
                smart = True
            out[sid] = _Stream(codec=normalize_codec(codec_raw), resolution=f"{w}x{h}" if w and h else None,
                               name=_text(sc, "channelName") or None, kbps=kbps, gop_s=gop_s, smart=smart)
        return out

    @staticmethod
    def _channel(cid: int, name: str, streams: dict[int, _Stream], *, online: bool | None = None,
                 ip_address: str | None = None, analog: bool | None = None) -> ChannelInfo:
        main, sub = streams.get(cid * 100 + 1) or _Stream(), streams.get(cid * 100 + 2)
        s = sub or _Stream()
        return ChannelInfo(
            channel=cid, name=name or main.name or f"Canal {cid}", online=online,
            has_sub=sub is not None or not streams, ip_address=ip_address, analog=analog,
            main_codec=main.codec, sub_codec=s.codec, main_resolution=main.resolution,
            sub_resolution=s.resolution, main_bitrate_kbps=main.kbps, sub_bitrate_kbps=s.kbps,
            gop_seconds=main.gop_s, smart_codec=main.smart)

    async def list_channels(self) -> list[ChannelInfo]:
        info = self._info or await self.probe()
        streams = await self._streaming()
        channels: list[ChannelInfo] = []
        if info.kind in ("nvr", "dvr"):
            analog = await self._analog_inputs() if info.kind == "dvr" else []
            for cid, name, online in analog:
                channels.append(self._channel(cid, name, streams, online=online, analog=True))
            proxies = await self._proxy_channels()
            status = await self._proxy_status() if proxies else {}
            taken = {c.channel for c in channels}
            for cid, name, ip in proxies:
                if cid in taken:
                    continue
                channels.append(self._channel(cid, name, streams, online=status.get(cid), ip_address=ip,
                                              analog=False if info.kind == "dvr" else None))
            if not channels:  # grabador sin InputProxy ni entradas: se deduce de los flujos
                for cid in sorted({sid // 100 for sid in streams}):
                    channels.append(self._channel(cid, "", streams))
        else:
            ch = self._channel(1, "", streams, online=True)
            if not ch.name or ch.name == "Canal 1":
                ch.name = info.name or self.device.name
            channels.append(ch)
        return sorted(channels, key=lambda c: c.channel)

    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        sid = int(channel) * 100 + (1 if stream == "main" else 2)
        resp = await self.http.get(f"/ISAPI/Streaming/channels/{sid}/picture")
        if not resp.content.startswith(b"\xff\xd8"):
            raise DeviceProtocolError(f"El equipo {self.http.label} no devolvió una imagen JPEG")
        return resp.content

    # ------------------------------------------------------------------ «Corregir códec»
    def _stream_resource(self, channel: int, stream: Literal["main", "sub"]) -> str:
        return f"/ISAPI/Streaming/channels/{int(channel) * 100 + (1 if stream == 'main' else 2)}"

    async def read_stream_config(self, channel: int, stream: Literal["main", "sub"] = "sub") -> StreamConfigBackup:
        resource = self._stream_resource(channel, stream)
        resp = await self.http.get(resource)
        root = parse_xml(resp.text, "StreamingChannel")
        codec = normalize_codec(_text(root, "Video/videoCodecType"))
        return StreamConfigBackup(vendor="hikvision", channel=int(channel), stream=stream, codec=codec,
                                  resource=resource, raw=resp.text, fmt="xml")

    async def set_stream_codec(self, channel: int, stream: Literal["main", "sub"], codec: str) -> StreamConfigBackup:
        """Cambia el códec del flujo con PUT del mismo recurso. Devuelve la copia de ANTES del cambio."""
        before = await self.read_stream_config(channel, stream)
        target = "H.264" if normalize_codec(codec) == "H.264" else codec
        new_xml, n = re.subn(r"(<videoCodecType>)[^<]*(</videoCodecType>)", rf"\g<1>{target}\g<2>", before.raw, count=1)
        if not n:
            raise DeviceProtocolError(f"{self.http.label}: el flujo no informa de su códec (videoCodecType)")
        # H.264+/Smart Codec fuera: si no, el equipo vuelve a poner GOP largo y variable
        new_xml = re.sub(r"(<SmartCodec>\s*<enabled>)true(</enabled>)", r"\g<1>false\g<2>", new_xml, count=1)
        await self._put_checked(before.resource, new_xml)
        return before

    async def restore_stream_config(self, backup: StreamConfigBackup) -> None:
        await self._put_checked(backup.resource, backup.raw)

    async def _put_checked(self, resource: str, xml_text: str) -> None:
        resp = await self.http.put(resource, xml_text.encode("utf-8"))
        if resp.content and b"ResponseStatus" in resp.content:
            root = parse_xml(resp.text, "ResponseStatus")
            code = _text(root, "statusCode")
            if code not in ("", "1", "7"):   # 1 = OK, 7 = OK y hace falta reiniciar
                raise DeviceProtocolError(f"{self.http.label} rechazó el cambio: {_text(root, 'subStatusCode')}",
                                          details={"status_code": code})

    # ------------------------------------------------------------------ TIME_READ
    async def device_time(self) -> DeviceTime:
        with Stopwatch() as sw:
            resp = await self.http.get("/ISAPI/System/time")
        root = parse_xml(resp.text, "System/time")
        tz = posix_offset(_text(root, "timeZone")) or timezone.utc
        dt = parse_device_datetime(_text(root, "localTime"), tz)
        if dt is None:
            raise DeviceProtocolError(f"{self.http.label} no devolvió su hora")
        mode_raw = _text(root, "timeMode").lower()
        mode: Literal["ntp", "manual", "unknown"] = "ntp" if mode_raw == "ntp" else \
            "manual" if mode_raw in ("manual", "timecorrect", "satellite") else "unknown"
        ntp = ""
        if mode == "ntp":
            try:
                r = await self.http.get("/ISAPI/System/time/ntpServers", ok_404=True)
                if r.status_code == 200:
                    nroot = parse_xml(r.text, "ntpServers")
                    ntp = _text(nroot, "NTPServer/hostName") or _text(nroot, "NTPServer/ipAddress")
            except DeviceError as exc:
                log.debug("ntpServers no disponible en %s: %s", self.http.label, exc)
        return DeviceTime(device_time=dt, measured_at=sw.midpoint, round_trip_ms=round(sw.round_trip_ms, 1),
                          time_mode=mode, ntp_server=ntp, source="isapi")

    # ------------------------------------------------------------------ SECURITY_READ
    async def security_settings(self, admin_username: str, admin_password: str) -> DeviceSecuritySettings:
        """Lee ajustes con credenciales de administrador TEMPORALES (no se guardan ni se registran)."""
        dev = self.device.model_copy(update={"username": admin_username})
        http = VendorHttp(dev, admin_password, timeout=self._timeout, transport=self._transport)
        out = DeviceSecuritySettings()
        try:
            async def flag(path: str, what: str) -> bool | None:
                r = await http.get(path, ok_404=True)
                if r.status_code == 404:
                    return None
                root = parse_xml(r.text, what)
                v = _bool(root, "enabled")
                out.raw[what] = "" if v is None else str(v).lower()
                return v

            out.telnet_enabled = await flag("/ISAPI/System/Network/telnetd", "telnetd")
            out.ssh_enabled = await flag("/ISAPI/System/Network/ssh", "ssh")
            out.upnp_enabled = await flag("/ISAPI/System/Network/UPnP", "UPnP")
            out.p2p_cloud_enabled = await flag("/ISAPI/System/Network/EZVIZ", "EZVIZ")
            r = await http.get("/ISAPI/Security/adminAccesses", ok_404=True)
            if r.status_code == 200:
                root = parse_xml(r.text, "adminAccesses")
                for proto in root.iter("AdminAccessProtocol"):
                    name = _text(proto, "protocol").upper()
                    enabled = _bool(proto, "enabled")
                    port = _text(proto, "portNo")
                    out.raw[f"port_{name.lower()}"] = port
                    if name == "HTTP":
                        out.http_enabled = True if enabled is None else enabled
                    elif name == "HTTPS":
                        out.https_enabled = enabled
                    elif name in ("DEV_MANAGE", "SDK"):
                        out.sdk_port_open = True if enabled is None else enabled
        finally:
            await http.aclose()
        return out

    async def aclose(self) -> None:
        await self.http.aclose()
