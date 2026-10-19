"""Hikvision: API ISAPI por HTTP con autenticación Digest (implementación propia).

Endpoints usados (todos de lectura salvo «Corregir códec»):
  GET /ISAPI/System/deviceInfo                          modelo, serie, firmware y fecha, MAC, tipo
  GET /ISAPI/ContentMgmt/InputProxy/channels[/status]   canales IP de un NVR/DVR (id, nombre, IP, en línea)
  GET /ISAPI/System/Video/inputs/channels               entradas analógicas de un DVR/HVR híbrido
  GET /ISAPI/Streaming/channels                         códec, resolución, tasa, GOP y Smart Codec por flujo
  GET /ISAPI/Streaming/channels/<N>0<1|2>/picture       snapshot JPEG (solo en memoria)
  GET/PUT /ISAPI/Streaming/channels/<N>02               «Corregir códec» (copia previa y deshacer)
  GET /ISAPI/System/time, /ISAPI/System/time/ntpServers hora y modo (TIME_READ)
  GET/PUT /ISAPI/System/time                            «Poner en hora con este PC» (TIME_WRITE: `localTime` y, si se
                                                        pide, `timeZone` en el mismo PUT)
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
from datetime import timedelta, timezone
from typing import Literal

import httpx

from vms.core.errors import DeviceAuthFailed, DeviceError, DeviceProtocolError, DeviceUnsupported
from vms.core.interfaces import ChannelInfo, DeviceInfo, DeviceSecuritySettings, DeviceTime
from vms.core.models import DeviceBase, Vendor

from vms.core.tzposix import PosixZone, fmt_hms

from ._http import VendorHttp
from . import clock as _clock
from .clock import Stopwatch, parse_naive_or_aware, posix_zone, round_offset, zone_matches
from .codec import StreamConfigBackup, normalize_codec
from .errors import (BasicNotAllowed, DeviceClockManaged, DeviceLocked, DevicePermissionDenied, DeviceTimeUnverified,
                     DeviceWriteDenied, DeviceZoneNotApplied, DeviceZoneUnsupported)

log = logging.getLogger("vms.vendors.hikvision")

NVR_TYPES = ("NVR", "IPDVR", "DVS")
DVR_TYPES = ("DVR", "HVR", "XVR", "HYBIRD", "HYBRID")
_BUILD_RE = re.compile(r"(?:build\s*)?(\d{2})(\d{2})(\d{2})\b", re.IGNORECASE)
_BUILD8_RE = re.compile(r"(?:build\s*)?(20\d{2})-?(\d{2})-?(\d{2})\b", re.IGNORECASE)


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


def hik_zone(zone: PosixZone) -> str | None:
    """Zona POSIX → el formato de `<timeZone>` de Hikvision, o None si ese formato no puede expresarla.

    Hikvision usa siempre el nombre «CST», el offset POSIX (signo al revés que en ISO) con «H:MM:SS» y, con horario
    de verano, «DST» + lo que se ADELANTA («DST01:00:00») y las dos reglas con la hora entera: Madrid es
    «CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00» (es lo que lee `posix_zone`). Solo se escribe lo que
    ese formato expresa sin dudas: de UTC−12 a UTC+14, adelantos de hasta 2 h y cambios a una hora del día (de 00:00
    a 23:59). Fuera de eso (p. ej. un cambio «a las 24:00» o «a las −1:00» de tzdata) no se adivina cómo lo guardaría
    el equipo; tampoco si el texto se leería de vuelta como otra zona."""
    std = zone.std
    if std.total_seconds() % 60 or not timedelta(hours=-12) <= std <= timedelta(hours=14):
        return None
    sign = "+" if std <= timedelta(0) else "-"                     # POSIX: al revés que en ISO (UTC+0 = «+0»)
    text = f"CST{sign}{fmt_hms(abs(std))}"
    if zone.dst is not None:
        if zone.start is None or zone.end is None:
            return None
        ahead = zone.dst - zone.std
        if not timedelta(0) < ahead <= timedelta(hours=2) or ahead.total_seconds() % 60:
            return None
        for rule in (zone.start, zone.end):
            if not timedelta(0) <= rule.at < timedelta(hours=24) or rule.at.total_seconds() % 60:
                return None
        text += (f"DST{fmt_hms(ahead, hours_width=2)},M{zone.start.month}.{zone.start.week}.{zone.start.weekday}/"
                 f"{fmt_hms(zone.start.at, hours_width=2)},M{zone.end.month}.{zone.end.week}.{zone.end.weekday}/"
                 f"{fmt_hms(zone.end.at, hours_width=2)}")
    return text if posix_zone(text) == zone else None   # se lee igual que la zona pedida, o no se escribe


def build_date(text: str) -> str:
    """«build 210812» → «2021-08-12» (la auditoría compara Hikvision por fecha de build). También admite
    «build 20210812» y «2021-08-12». Si no se entiende, «» (la auditoría dirá «Desconocido»)."""
    m8 = _BUILD8_RE.search(text or "")
    if m8:
        yyyy, mm, dd = m8.groups()
    else:
        m = _BUILD_RE.search(text or "")
        if not m:
            return ""
        yy, mm, dd = m.groups()
        yyyy = f"20{yy}"
    if not (1 <= int(mm) <= 12 and 1 <= int(dd) <= 31):
        return ""
    return f"{yyyy}-{mm}-{dd}"


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

    async def _put_checked(self, resource: str, xml_text: str, *, http: VendorHttp | None = None) -> None:
        resp = await (http or self.http).put(resource, xml_text.encode("utf-8"))
        if resp.content and b"ResponseStatus" in resp.content:
            root = parse_xml(resp.text, "ResponseStatus")
            code = _text(root, "statusCode")
            if code not in ("", "1", "7") and _text(root, "subStatusCode").lower() == "lowprivilege":
                # firmwares que dicen «sin permiso» con un 200: igual que el 403 `lowPrivilege` (no es un fallo del
                # equipo; con la hora, el usuario de solo lectura)
                raise DevicePermissionDenied(self.http.label, resource)
            if code not in ("", "1", "7"):   # 1 = OK, 7 = OK y hace falta reiniciar
                raise DeviceProtocolError(f"{self.http.label} rechazó el cambio: {_text(root, 'subStatusCode')}",
                                          details={"status_code": code})

    # ------------------------------------------------------------------ TIME_READ
    async def device_time(self) -> DeviceTime:
        return await self._read_time(self.http)

    async def _read_time(self, http: VendorHttp) -> DeviceTime:
        with Stopwatch() as sw:
            resp = await http.get("/ISAPI/System/time")
        root = parse_xml(resp.text, "System/time")
        local = parse_naive_or_aware(_text(root, "localTime"))
        if local is None:
            raise DeviceProtocolError(f"{self.http.label} no devolvió su hora")
        if local.tzinfo is None:
            # sin offset en localTime: se resuelve con la zona POSIX del equipo, horario de verano incluido;
            # si la regla no se entiende no se adivina (sería un desfase falso de una hora en verano)
            zone = posix_zone(_text(root, "timeZone"))
            offset = zone.offset_at_local(local) if zone is not None else None
            if offset is None:
                raise DeviceProtocolError(f"{self.http.label} dio su hora sin zona y su zona horaria "
                                          f"({_text(root, 'timeZone')[:40] or 'vacía'}) no se entiende")
            local = local.replace(tzinfo=timezone(offset))
        dt = local
        utc_offset = dt.utcoffset()
        # «satellite» (GPS), «timecorrect» (la da el grabador), «SDK»/«ONVIF»/«platform» (la pone un programa):
        # no es hora puesta a mano ni NTP del equipo → «unknown» (sin aviso de deriva)
        mode_raw = _text(root, "timeMode").lower()
        mode: Literal["ntp", "manual", "unknown"] = "ntp" if mode_raw == "ntp" else \
            "manual" if mode_raw == "manual" else "unknown"
        ntp = await self._ntp_server(http) if mode == "ntp" else ""
        return DeviceTime(device_time=dt, measured_at=sw.midpoint, round_trip_ms=round(sw.round_trip_ms, 1),
                          time_mode=mode, ntp_server=ntp, source="isapi", zone=_text(root, "timeZone")[:64],
                          utc_offset_s=round_offset(utc_offset) if utc_offset is not None else None)

    async def _ntp_server(self, http: VendorHttp) -> str:
        try:
            r = await http.get("/ISAPI/System/time/ntpServers", ok_404=True)
            if r.status_code == 200:
                nroot = parse_xml(r.text, "ntpServers")
                return _text(nroot, "NTPServer/hostName") or _text(nroot, "NTPServer/ipAddress")
        except DeviceError as exc:
            log.debug("ntpServers no disponible en %s: %s", self.http.label, exc)
        return ""

    # ------------------------------------------------------------------ TIME_WRITE
    async def set_device_time(self, admin_username: str = "", admin_password: str = "", *,
                              zone: str = "") -> DeviceTime:
        """«Poner en hora con este PC» (CONTRATO §18.3): GET /ISAPI/System/time, se cambia `<localTime>` (la hora de
        este PC, con el mismo formato que dio el equipo: con desfase o sin él) y PUT del mismo XML, así se conservan
        el espacio de nombres y los nodos de cada firmware. `timeMode` no se toca. Después se lee de nuevo la hora
        para comprobarla.

        Zona: sin `zone`, `<timeZone>` no se toca y la hora va en la zona del propio equipo. Con `zone` (POSIX, la de
        este PC) se cambia también `<timeZone>` (formato de Hikvision, `hik_zone`) en el MISMO PUT y la hora va ya
        en la zona nueva: así el equipo la entiende igual tanto si mira el desfase de `localTime` como si no. Si esa
        zona no se puede expresar en su formato, `DeviceZoneUnsupported` sin escribir nada; también si el equipo
        rechaza ese PUT (`<ResponseStatus>` con error, p. ej. `badParameters`, o un estado HTTP de error): no se
        aplicó nada. Si al leer de nuevo el equipo tiene otra zona (o otro desfase), `DeviceZoneNotApplied` con la
        lectura.

        Solo con la hora puesta a mano (`manual`): con NTP, SDK (HikCentral, iVMS-4200), `platform`, `timecorrect`
        (la pone el grabador), `satellite` u ONVIF la pone otro sistema y se lanza `DeviceClockManaged` sin
        escribir nada. Un 403 `lowPrivilege` al escribir → `DeviceWriteDenied` (usuario de solo lectura); también un
        401 en el PUT, porque la misma contraseña acaba de valer en el GET (no se funde el cliente), con
        `details["auth_rejection"]` (el equipo pudo contarlo como intento fallido) y los intentos que le quedan.
        Con `admin_username`, credenciales de administrador temporales: solo Digest, no se guardan."""
        http, temp = self.http, None
        if admin_username:
            dev = self.device.model_copy(update={"username": admin_username})
            http = temp = VendorHttp(dev, admin_password, timeout=self._timeout, transport=self._transport,
                                     allow_basic=False)
        try:
            try:
                resp = await http.get("/ISAPI/System/time")
            except DevicePermissionDenied as exc:      # ni siquiera puede leerla: menos aún cambiarla
                raise DeviceWriteDenied(self.http.label, details={"status": 403}) from exc
            raw = resp.text
            root = parse_xml(raw, "System/time")
            mode = _text(root, "timeMode")
            if mode and mode.lower() != "manual":
                ntp = await self._ntp_server(http) if mode.lower() == "ntp" else ""
                raise DeviceClockManaged(self.http.label, mode, ntp)
            current = parse_naive_or_aware(_text(root, "localTime"))
            if current is None:
                raise DeviceProtocolError(f"{self.http.label} no devolvió su hora")
            zone_text = _text(root, "timeZone")
            target: PosixZone | None = None
            target_text = ""
            if zone:
                target = posix_zone(zone)
                target_text = (hik_zone(target) if target is not None else None) or ""
                if target is None or not target_text:
                    raise DeviceZoneUnsupported(self.http.label, zone, "el formato de Hikvision no admite esas reglas")
            device_zone = target if target is not None else posix_zone(zone_text)
            now = _clock.utc_now()
            offset = device_zone.offset_at_utc(now) if device_zone is not None else None
            if offset is None:
                offset = current.utcoffset()   # zona sin entender: el desfase que trae su propia hora
            if offset is None:
                raise DeviceProtocolError(f"{self.http.label}: su zona horaria ({zone_text[:40] or 'vacía'}) no se "
                                          "entiende y no se escribe una hora que podría quedar desplazada")
            # al segundo más cercano (ISAPI no admite fracciones): el error queda por debajo de medio segundo
            local = (now + timedelta(milliseconds=500)).replace(microsecond=0).astimezone(timezone(offset))
            value = local.isoformat(timespec="seconds") if current.tzinfo is not None else \
                local.replace(tzinfo=None).isoformat(timespec="seconds")
            new_xml, n = re.subn(r"(<localTime>)[^<]*(</localTime>)", rf"\g<1>{value}\g<2>", raw, count=1)
            if not n:
                raise DeviceProtocolError(f"{self.http.label} no informa de su hora (localTime)")
            if target_text:
                # en el mismo PUT que la hora: una escritura, y la hora ya va en la zona nueva
                new_xml, n = re.subn(r"(<timeZone>)[^<]*(</timeZone>)", rf"\g<1>{target_text}\g<2>", new_xml, count=1)
                if not n:
                    new_xml = re.sub(r"(</localTime>)", rf"\g<1><timeZone>{target_text}</timeZone>", new_xml, count=1)
            authed = http.credentialed_requests > 0     # el GET de la hora acaba de valer CON estas credenciales
            try:
                await self._put_checked("/ISAPI/System/time", new_xml, http=http)
            except DevicePermissionDenied as exc:       # 403 `lowPrivilege`: usuario de solo lectura
                raise DeviceWriteDenied(self.http.label, details={"status": 403}) from exc
            except DeviceAuthFailed as exc:
                if isinstance(exc, (DeviceLocked, BasicNotAllowed)):
                    raise
                if exc.details.get("status") == 403:    # un 403 sin más: falta de permisos
                    http.keep_credentials()
                    raise DeviceWriteDenied(self.http.label, details={"status": 403}) from exc
                if authed:
                    # un 401 de un firmware que contesta así a la escritura de un usuario sin permiso: la misma
                    # contraseña acaba de valer para leer, no es mala (ni se funde). Pero es la respuesta de una
                    # contraseña mala y el equipo puede contarla como intento fallido: quien llama no insiste
                    http.keep_credentials()
                    raise DeviceWriteDenied(self.http.label, details={
                        "status": 401, "auth_rejection": True,
                        "remaining_attempts": exc.details.get("remaining_attempts")}) from exc
                raise
            except (DeviceProtocolError, DeviceUnsupported) as exc:
                if not target_text or not (exc.details.get("status") or exc.details.get("status_code")):
                    raise
                # rechazó el PUT que lleva la zona nueva (`<ResponseStatus>` `badParameters` con un 200 o un 400,
                # `notSupport`…): el PUT entero no se aplicó, ni la hora ni la zona. Como en ONVIF, no se repite sin
                # la zona por su cuenta: se dice que no la admite y la persona vuelve a pulsar sin la casilla (si
                # no, el 502 genérico no lo decía y la casilla, marcada por defecto, fallaba igual cada vez)
                raise DeviceZoneUnsupported(self.http.label, zone, exc.message) from exc
            if target_text:
                log.info("Hora y zona de %s puestas con las de este PC (zona %s → %s)", self.http.label,
                         zone_text[:40] or "-", target_text)
            else:
                log.info("Hora de %s puesta con la de este PC (zona del equipo %s sin cambios)", self.http.label,
                         zone_text[:40] or "-")
            try:
                after = await self._read_time(http)
            except DeviceError as exc:      # escrita: lo que falla es solo la comprobación
                raise DeviceTimeUnverified(self.http.label, exc, previous_zone=zone_text) from exc
            after = after.model_copy(update={"previous_zone": zone_text[:64]})
            if target is not None and not zone_matches(target, after.zone, after.utc_offset_s, after.device_time):
                raise DeviceZoneNotApplied(self.http.label, target_text, after)
            return after
        finally:
            if temp is not None:
                await temp.aclose()

    # ------------------------------------------------------------------ SECURITY_READ
    async def security_settings(self, admin_username: str, admin_password: str) -> DeviceSecuritySettings:
        """Lee ajustes con credenciales de administrador TEMPORALES (no se guardan ni se registran).

        Nunca con Basic, aunque el equipo lo tenga permitido: la contraseña de administrador no viaja en claro.
        Un recurso que el firmware no tiene (404, o 403 con `notSupport`) queda en None sin abortar el resto."""
        dev = self.device.model_copy(update={"username": admin_username})
        http = VendorHttp(dev, admin_password, timeout=self._timeout, transport=self._transport, allow_basic=False)
        out = DeviceSecuritySettings()
        try:
            async def flag(path: str, what: str) -> bool | None:
                try:
                    r = await http.get(path, ok_404=True)
                except DeviceUnsupported:
                    return None
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
            try:
                r = await http.get("/ISAPI/Security/adminAccesses", ok_404=True)
            except DeviceUnsupported:
                r = None
            if r is not None and r.status_code == 200:
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
