"""Dahua: API CGI por HTTP con autenticación Digest (implementación propia).

Endpoints usados (respuestas de texto «clave=valor» separadas por CRLF):
  /cgi-bin/magicBox.cgi?action=getDeviceType | getSerialNo | getSoftwareVersion | getMachineName
  /cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxRemoteInputChannels | VideoInputChannels
  /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle | Encode | Network | NTP | …
  /cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uniqueChannels[0]=-1      (NVR/XVR: canales IP)
  /cgi-bin/snapshot.cgi?channel=N[&type=1]                                          (JPEG en memoria)
  /cgi-bin/configManager.cgi?action=setConfig&Encode[i].ExtraFormat[0].Video.Compression=H.264  («Corregir códec»)
  /cgi-bin/global.cgi?action=getCurrentTime                                          (TIME_READ)

Los índices de la configuración (table.X[i]) empiezan en 0; el canal de usuario y de RTSP («channel=N»)
empieza en 1. En un XVR los canales IP van detrás de los analógicos (4 analógicos + 4 IP desde el 5): el
canal se toma siempre del índice que da el equipo (`LogicDeviceManager`), nunca se supone (no verificado
con hardware).
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import httpx

from vms.core.errors import DeviceError, DeviceProtocolError
from vms.core.interfaces import ChannelInfo, DeviceInfo, DeviceSecuritySettings, DeviceTime
from vms.core.models import DeviceBase, Vendor

from ._http import VendorHttp
from .clock import Stopwatch, local_tz, parse_naive_or_aware, round_offset
from .codec import StreamConfigBackup, normalize_codec

log = logging.getLogger("vms.vendors.dahua")

NVR_MARKERS = ("NVR", "IVSS", "EVS")
XVR_MARKERS = ("XVR",)
DVR_MARKERS = ("HCVR", "DVR", "HDCVI")
_INDEX_RE = re.compile(r"\[(\d+)\]")
_DATE_RE = re.compile(r"build[:\s]*(\d{4})-(\d{2})-(\d{2})", re.IGNORECASE)


def parse_kv(text: str) -> dict[str, str]:
    """«a=b\\r\\nc=d» → {"a": "b", "c": "d"}. Ignora líneas sin «=»."""
    out: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def _codec(value: str | None) -> str | None:
    return normalize_codec(value)


def _indexed(kv: dict[str, str], prefix: str, suffix: str) -> dict[int, str]:
    """Valores de claves «<prefix>[i]<suffix>» indexados por i."""
    out: dict[int, str] = {}
    for key, value in kv.items():
        if key.startswith(prefix) and key.endswith(suffix):
            m = _INDEX_RE.match(key[len(prefix):])
            if m and key[len(prefix) + m.end():] == suffix:
                out[int(m.group(1))] = value
    return out


def firmware_date(version: str) -> str:
    """«4.001.0000000.1,build:2021-06-04» → «2021-06-04»."""
    m = _DATE_RE.search(version or "")
    return "-".join(m.groups()) if m else ""


def _int(value: str | None) -> int | None:
    return int(value) if value and value.strip().isdigit() else None


# Índice de `NTP.TimeZone` → horas respecto a UTC (tabla de la API HTTP de Dahua; no verificado con hardware).
DAHUA_TIME_ZONES: tuple[float, ...] = (
    0, 1, 2, 3, 3.5, 4, 4.5, 5, 5.5, 5.75, 6, 6.5, 7, 8, 9, 9.5, 10, 11, 12, 13,
    -1, -2, -3, -3.5, -4, -5, -6, -7, -8, -9, -10, -11, -12)


def _dst_point(loc: dict[str, str], which: str, year: int) -> datetime | None:
    """Inicio o fin del horario de verano de `Locales` («DSTStart.Month/Week/Day/Hour/Minute»). `Week` 1..4 o −1
    (última) con `Day` = día de la semana (0 = domingo); `Week` 0 = fecha fija (`Day` = día del mes)."""
    def get(field: str) -> int | None:
        raw = loc.get(f"table.Locales.{which}.{field}", "").strip()
        return int(raw) if raw.lstrip("-").isdigit() else None
    month, week, day, hour, minute = get("Month"), get("Week"), get("Day"), get("Hour"), get("Minute")
    if month is None or week is None or day is None or not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    try:
        if week == 0:
            d = date(year, month, day)
        else:
            if not 0 <= day <= 6 or week not in (1, 2, 3, 4, 5, -1):
                return None
            py_wd = (day - 1) % 7
            first = date(year, month, 1)
            dom = 1 + (py_wd - first.weekday()) % 7 + 7 * ((5 if week == -1 else week) - 1)
            while dom > last:
                dom -= 7
            d = date(year, month, dom)
        return datetime(d.year, d.month, d.day, hour or 0, minute or 0)
    except ValueError:
        return None


class DahuaClient:
    vendor: Vendor = "dahua"

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.device = device
        self._timeout = timeout
        self._transport = transport
        self.http = VendorHttp(device, password, timeout=timeout, transport=transport)
        self._info: DeviceInfo | None = None
        if device.vendor and device.vendor != "dahua":
            self.vendor = device.vendor   # Imou, Amcrest, Lorex… comparten la API

    async def _cgi(self, script: str, params: list[tuple[str, str]], http: VendorHttp | None = None) -> dict[str, str]:
        resp = await (http or self.http).get(f"/cgi-bin/{script}", params=params)
        text = resp.text
        if text.startswith("Error"):
            raise DeviceProtocolError(f"El equipo Dahua no admite {script} ({text.strip()[:60]})")
        return parse_kv(text)

    async def _config(self, name: str, http: VendorHttp | None = None) -> dict[str, str]:
        return await self._cgi("configManager.cgi", [("action", "getConfig"), ("name", name)], http)

    async def probe(self) -> DeviceInfo:
        kv = await self._cgi("magicBox.cgi", [("action", "getDeviceType")])
        dtype = kv.get("type", "")
        if not dtype:
            raise DeviceProtocolError("El equipo respondió, pero no parece un Dahua (magicBox)")
        serial = firmware = mac = ""
        try:
            serial = (await self._cgi("magicBox.cgi", [("action", "getSerialNo")])).get("sn", "")
        except DeviceProtocolError as exc:
            log.debug("getSerialNo no disponible: %s", exc)
        try:
            firmware = (await self._cgi("magicBox.cgi", [("action", "getSoftwareVersion")])).get("version", "")
        except DeviceProtocolError as exc:
            log.debug("getSoftwareVersion no disponible: %s", exc)
        try:
            net = await self._config("Network")
            mac = next((v for k, v in net.items() if k.endswith(".PhysicalAddress") and v), "").lower()
        except DeviceProtocolError as exc:
            log.debug("Network no disponible: %s", exc)
        up = dtype.upper()
        kind: Literal["camera", "nvr", "dvr", "xvr"]
        if any(m in up for m in XVR_MARKERS):
            kind = "xvr"
        elif any(m in up for m in DVR_MARKERS):
            kind = "dvr"
        elif any(m in up for m in NVR_MARKERS):
            kind = "nvr"
        else:
            kind = "camera"
        info = DeviceInfo(vendor=self.vendor, kind=kind, model=dtype, serial=serial,
                          firmware=firmware,
                          firmware_date=firmware_date(firmware), mac=mac, name=serial)
        info.channel_count = await self._channel_count() if kind != "camera" else 1
        self._info = info
        return info

    async def _product(self, name: str) -> int | None:
        try:
            kv = await self._cgi("magicBox.cgi", [("action", "getProductDefinition"), ("name", name)])
        except DeviceProtocolError as exc:
            log.debug("%s no disponible: %s", name, exc)
            return None
        return _int(kv.get(f"table.{name}"))

    async def _channel_count(self) -> int:
        remote = await self._product("MaxRemoteInputChannels") or 0
        local = await self._product("VideoInputChannels") or 0
        titles = await self._titles()
        return max(remote + local, len(titles))

    async def _titles(self) -> dict[int, str]:
        kv = await self._config("ChannelTitle")
        return _indexed(kv, "table.ChannelTitle", ".Name")

    async def list_channels(self) -> list[ChannelInfo]:
        info = self._info or await self.probe()
        titles = await self._titles()
        count = max(info.channel_count, len(titles), 1) if info.kind != "camera" else 1
        encode: dict[str, str] = {}
        try:
            encode = await self._config("Encode")
        except DeviceError as exc:
            log.warning("No se pudo leer la configuración de códec de %s: %s", self.http.label, exc)
        smart: dict[int, str] = {}
        try:
            smart = _indexed(await self._config("SmartEncode"), "table.SmartEncode", ".Enable")
        except DeviceError:
            log.debug("SmartEncode no disponible en %s", self.http.label)
        online: dict[int, bool] = {}
        ip_chans: set[int] = set()
        if info.kind != "camera":
            try:
                kv = await self._cgi("LogicDeviceManager.cgi", [("action", "getCameraState"),
                                                                ("uniqueChannels[0]", "-1")])
                chans = _indexed(kv, "states", ".channel")
                states = _indexed(kv, "states", ".connectionState")
                for i, ch in chans.items():
                    if ch.lstrip("-").isdigit() and int(ch) >= 0:
                        ip_chans.add(int(ch))
                        if i in states:
                            online[int(ch)] = states[i].lower() == "connected"
            except DeviceError as exc:
                log.info("getCameraState no disponible en %s: %s", self.http.label, exc)
        local = (await self._product("VideoInputChannels") or 0) if info.kind in ("xvr", "dvr") else 0

        def fmt(idx: int, kind: str, field: str) -> str | None:
            return encode.get(f"table.Encode[{idx}].{kind}[0].{field}")

        def res(idx: int, kind: str) -> str | None:
            w, h = fmt(idx, kind, "Video.Width"), fmt(idx, kind, "Video.Height")
            return f"{w}x{h}" if w and h else None

        def gop(idx: int) -> float | None:
            g, f = _int(fmt(idx, "MainFormat", "Video.GOP")), _int(fmt(idx, "MainFormat", "Video.FPS"))
            return round(g / f, 2) if g and f else None

        out: list[ChannelInfo] = []
        for idx in range(count):
            sub_enabled = fmt(idx, "ExtraFormat", "VideoEnable")
            analog: bool | None = None
            if info.kind in ("xvr", "dvr"):
                analog = idx not in ip_chans if ip_chans else (idx < local if local else None)
            out.append(ChannelInfo(
                channel=idx + 1,
                name=titles.get(idx) or f"Canal {idx + 1}",
                online=online.get(idx) if info.kind != "camera" else True,
                has_sub=(sub_enabled or "true").lower() != "false",
                main_codec=_codec(fmt(idx, "MainFormat", "Video.Compression")),
                sub_codec=_codec(fmt(idx, "ExtraFormat", "Video.Compression")),
                main_resolution=res(idx, "MainFormat"), sub_resolution=res(idx, "ExtraFormat"),
                main_bitrate_kbps=_int(fmt(idx, "MainFormat", "Video.BitRate")),
                sub_bitrate_kbps=_int(fmt(idx, "ExtraFormat", "Video.BitRate")),
                gop_seconds=gop(idx),
                smart_codec=(smart[idx].lower() == "true") if idx in smart else None,
                analog=analog))
        return out

    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        # «type=1» = subflujo (PLAN-V2 §3.2 punto 5); sin type, el equipo da el principal
        params = [("channel", str(int(channel)))] + ([("type", "1")] if stream == "sub" else [])
        resp = await self.http.get("/cgi-bin/snapshot.cgi", params=params)
        if not resp.content.startswith(b"\xff\xd8"):
            raise DeviceProtocolError(f"El equipo {self.http.label} no devolvió una imagen JPEG")
        return resp.content

    # ------------------------------------------------------------------ «Corregir códec»
    @staticmethod
    def _key(channel: int, stream: Literal["main", "sub"]) -> str:
        fmt = "MainFormat" if stream == "main" else "ExtraFormat"
        return f"Encode[{int(channel) - 1}].{fmt}[0]"

    async def read_stream_config(self, channel: int, stream: Literal["main", "sub"] = "sub") -> StreamConfigBackup:
        kv = await self._config("Encode")
        prefix = "table." + self._key(channel, stream) + "."
        lines = {k: v for k, v in kv.items() if k.startswith(prefix)}
        if not lines:
            raise DeviceProtocolError(f"{self.http.label} no tiene el canal {channel}")
        raw = "".join(f"{k}={v}\r\n" for k, v in sorted(lines.items()))
        return StreamConfigBackup(vendor="dahua", channel=int(channel), stream=stream,
                                  codec=_codec(lines.get(prefix + "Video.Compression")),
                                  resource=self._key(channel, stream), raw=raw, fmt="txt")

    async def _set(self, pairs: list[tuple[str, str]]) -> None:
        resp = await self.http.get("/cgi-bin/configManager.cgi", params=[("action", "setConfig"), *pairs])
        if resp.text.strip().upper() != "OK":
            raise DeviceProtocolError(f"{self.http.label} rechazó el cambio ({resp.text.strip()[:60]})")

    async def set_stream_codec(self, channel: int, stream: Literal["main", "sub"], codec: str) -> StreamConfigBackup:
        before = await self.read_stream_config(channel, stream)
        target = "H.264" if normalize_codec(codec) == "H.264" else codec
        await self._set([(f"{before.resource}.Video.Compression", target)])
        return before

    async def restore_stream_config(self, backup: StreamConfigBackup) -> None:
        if not backup.codec:
            raise DeviceProtocolError("La copia no tiene el códec anterior")
        prev = parse_kv(backup.raw).get(f"table.{backup.resource}.Video.Compression") or backup.codec
        await self._set([(f"{backup.resource}.Video.Compression", prev)])

    # ------------------------------------------------------------------ TIME_READ
    async def device_time(self) -> DeviceTime:
        """Hora del equipo. `getCurrentTime` da la hora LOCAL sin zona («2011-7-3 21:02:32» en el ejemplo de la API):
        la zona sale de `NTP.TimeZone` (índice de la tabla de Dahua) y el horario de verano de `Locales`. Si no se
        pueden leer, se supone la zona del PC (misma tienda) y `utc_offset_s` queda en None. No verificado con
        hardware (CONTRATO §18.3, docs/COMPATIBILIDAD.md)."""
        with Stopwatch() as sw:
            kv = await self._cgi("global.cgi", [("action", "getCurrentTime")])
        local = parse_naive_or_aware(kv.get("result", ""))
        if local is None:
            raise DeviceProtocolError(f"{self.http.label} no devolvió su hora")
        mode: Literal["ntp", "manual", "unknown"] = "unknown"
        server = ""
        offset: timedelta | None = None
        try:
            ntp = await self._config("NTP")
            enable = ntp.get("table.NTP.Enable", "").lower()
            mode = "ntp" if enable == "true" else "manual" if enable == "false" else "unknown"
            server = ntp.get("table.NTP.Address", "")
            offset = await self._zone_offset(local, ntp)
        except DeviceError as exc:
            log.debug("NTP no disponible en %s: %s", self.http.label, exc)
        if local.tzinfo is not None:
            dt = local
            offset = local.utcoffset()
        elif offset is not None:
            dt = local.replace(tzinfo=timezone(offset))
        else:
            dt = local.replace(tzinfo=local_tz())
        return DeviceTime(device_time=dt, measured_at=sw.midpoint, round_trip_ms=round(sw.round_trip_ms, 1),
                          time_mode=mode, ntp_server=server, source="cgi",
                          utc_offset_s=round_offset(offset) if offset is not None else None)

    async def _zone_offset(self, local: datetime, ntp: dict[str, str]) -> timedelta | None:
        """Offset de la hora local del equipo: `NTP.TimeZone` + horario de verano de `Locales`. None si no se sabe."""
        idx = _int(ntp.get("table.NTP.TimeZone"))
        if idx is None or not 0 <= idx < len(DAHUA_TIME_ZONES):
            return None
        std = timedelta(hours=DAHUA_TIME_ZONES[idx])
        try:
            loc = await self._config("Locales")
        except DeviceError as exc:
            log.debug("Locales no disponible en %s: %s", self.http.label, exc)
            return None
        dst_on = loc.get("table.Locales.DSTEnable", "").lower()
        if dst_on == "false":
            return std
        if dst_on != "true":
            return None
        start, end = _dst_point(loc, "DSTStart", local.year), _dst_point(loc, "DSTEnd", local.year)
        if start is None or end is None:
            return None
        naive = local.replace(tzinfo=None)
        in_dst = start <= naive < end if start < end else (naive >= start or naive < end)
        return std + timedelta(hours=1) if in_dst else std

    # ------------------------------------------------------------------ SECURITY_READ
    async def security_settings(self, admin_username: str, admin_password: str) -> DeviceSecuritySettings:
        """Con credenciales de administrador TEMPORALES (no se guardan ni se registran). Nunca con Basic: la
        contraseña de administrador no viaja en claro aunque el equipo lo tenga permitido."""
        dev = self.device.model_copy(update={"username": admin_username})
        http = VendorHttp(dev, admin_password, timeout=self._timeout, transport=self._transport, allow_basic=False)
        out = DeviceSecuritySettings()
        try:
            async def enabled(name: str) -> bool | None:
                try:
                    kv = await self._config(name, http)
                except DeviceProtocolError:
                    return None
                v = kv.get(f"table.{name}.Enable", "").lower()
                out.raw[name] = v
                return True if v == "true" else False if v == "false" else None

            out.telnet_enabled = await enabled("Telnet")
            out.ssh_enabled = await enabled("SSHD")
            out.upnp_enabled = await enabled("UPnP")
            out.p2p_cloud_enabled = await enabled("T2UServer")
            try:
                web = await self._config("Web", http)
                out.http_enabled = web.get("table.Web.Enable", "true").lower() != "false"
                https = web.get("table.Web.SSLEnable") or web.get("table.Web.HttpsEnable")
                out.https_enabled = None if https is None else https.lower() == "true"
            except DeviceProtocolError:
                pass
        finally:
            await http.aclose()
        return out

    async def aclose(self) -> None:
        await self.http.aclose()
