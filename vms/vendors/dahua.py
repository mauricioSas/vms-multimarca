"""Dahua: API CGI por HTTP con autenticación Digest (implementación propia).

Endpoints usados (respuestas de texto «clave=valor» separadas por CRLF):
  /cgi-bin/magicBox.cgi?action=getDeviceType | getSystemInfo | getSerialNo | getSoftwareVersion
  /cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxRemoteInputChannels   (NVR)
  /cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle | Encode
  /cgi-bin/LogicDeviceManager.cgi?action=getCameraState&uniqueChannels[0]=-1     (NVR)
  /cgi-bin/snapshot.cgi?channel=N[&type=1]                                         (JPEG en memoria)

Los índices de la configuración (table.X[i]) empiezan en 0; el canal de usuario y de RTSP
(«channel=N») empieza en 1.
"""
from __future__ import annotations

import logging
import re
from typing import Literal

import httpx

from vms.core.errors import DeviceError, DeviceProtocolError
from vms.core.interfaces import ChannelInfo, DeviceInfo
from vms.core.models import DeviceBase, Vendor

from ._http import VendorHttp

log = logging.getLogger("vms.vendors.dahua")

NVR_MARKERS = ("NVR", "DVR", "XVR", "HCVR", "HDCVI", "IVSS", "EVS")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def parse_kv(text: str) -> dict[str, str]:
    """«a=b\\r\\nc=d» → {"a": "b", "c": "d"}. Ignora líneas sin «=»."""
    out: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def _codec(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().upper().replace(".", "")
    if v.startswith("H264"):
        return "H.264"
    if v.startswith("H265") or v.startswith("HEVC"):
        return "H.265"
    return value.strip()


def _indexed(kv: dict[str, str], prefix: str, suffix: str) -> dict[int, str]:
    """Valores de claves «<prefix>[i]<suffix>» indexados por i."""
    out: dict[int, str] = {}
    for key, value in kv.items():
        if key.startswith(prefix) and key.endswith(suffix):
            m = _INDEX_RE.match(key[len(prefix):])
            if m and key[len(prefix) + m.end():] == suffix:
                out[int(m.group(1))] = value
    return out


class DahuaClient:
    vendor: Vendor = "dahua"

    def __init__(self, device: DeviceBase, password: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.device = device
        self.http = VendorHttp(device, password, timeout=timeout, transport=transport)
        self._info: DeviceInfo | None = None

    async def _cgi(self, script: str, params: list[tuple[str, str]]) -> dict[str, str]:
        resp = await self.http.get(f"/cgi-bin/{script}", params=params)
        text = resp.text
        if text.startswith("Error"):
            raise DeviceProtocolError(f"El equipo Dahua no admite {script} ({text.strip()[:60]})")
        return parse_kv(text)

    async def probe(self) -> DeviceInfo:
        kv = await self._cgi("magicBox.cgi", [("action", "getDeviceType")])
        dtype = kv.get("type", "")
        if not dtype:
            raise DeviceProtocolError("El equipo respondió, pero no parece un Dahua (magicBox)")
        serial = firmware = ""
        try:
            serial = (await self._cgi("magicBox.cgi", [("action", "getSerialNo")])).get("sn", "")
        except DeviceProtocolError as exc:
            log.debug("getSerialNo no disponible: %s", exc)
        try:
            firmware = (await self._cgi("magicBox.cgi", [("action", "getSoftwareVersion")])).get("version", "")
        except DeviceProtocolError as exc:
            log.debug("getSoftwareVersion no disponible: %s", exc)
        kind: Literal["camera", "nvr", "unknown"] = (
            "nvr" if any(m in dtype.upper() for m in NVR_MARKERS) else "camera")
        info = DeviceInfo(vendor="dahua", kind=kind, model=dtype, serial=serial, firmware=firmware,
                          name=serial)
        if kind == "nvr":
            info.channel_count = await self._channel_count()
        else:
            info.channel_count = 1
        self._info = info
        return info

    async def _channel_count(self) -> int:
        try:
            kv = await self._cgi("magicBox.cgi", [("action", "getProductDefinition"),
                                                  ("name", "MaxRemoteInputChannels")])
            value = kv.get("table.MaxRemoteInputChannels")
            if value and value.isdigit():
                return int(value)
        except DeviceProtocolError as exc:
            log.debug("MaxRemoteInputChannels no disponible: %s", exc)
        titles = await self._titles()
        return len(titles)

    async def _titles(self) -> dict[int, str]:
        kv = await self._cgi("configManager.cgi", [("action", "getConfig"), ("name", "ChannelTitle")])
        return _indexed(kv, "table.ChannelTitle", ".Name")

    async def list_channels(self) -> list[ChannelInfo]:
        info = self._info or await self.probe()
        titles = await self._titles()
        count = max(info.channel_count, len(titles), 1) if info.kind == "nvr" else 1
        encode: dict[str, str] = {}
        try:
            encode = await self._cgi("configManager.cgi", [("action", "getConfig"), ("name", "Encode")])
        except DeviceError as exc:
            log.warning("No se pudo leer la configuración de códec de %s: %s", self.http.label, exc)
        online: dict[int, bool] = {}
        if info.kind == "nvr":
            try:
                kv = await self._cgi("LogicDeviceManager.cgi", [("action", "getCameraState"),
                                                                ("uniqueChannels[0]", "-1")])
                chans = _indexed(kv, "states", ".channel")
                states = _indexed(kv, "states", ".connectionState")
                for i, ch in chans.items():
                    if ch.lstrip("-").isdigit() and i in states:
                        online[int(ch)] = states[i].lower() == "connected"
            except DeviceError as exc:
                log.info("getCameraState no disponible en %s: %s", self.http.label, exc)
        out: list[ChannelInfo] = []
        def fmt(idx: int, kind: str, field: str) -> str | None:
            return encode.get(f"table.Encode[{idx}].{kind}[0].{field}")

        def res(idx: int, kind: str) -> str | None:
            w, h = fmt(idx, kind, "Video.Width"), fmt(idx, kind, "Video.Height")
            return f"{w}x{h}" if w and h else None

        for idx in range(count):
            sub_enabled = fmt(idx, "ExtraFormat", "VideoEnable")
            out.append(ChannelInfo(
                channel=idx + 1,
                name=titles.get(idx) or f"Canal {idx + 1}",
                online=online.get(idx) if info.kind == "nvr" else True,
                has_sub=(sub_enabled or "true").lower() != "false",
                main_codec=_codec(fmt(idx, "MainFormat", "Video.Compression")),
                sub_codec=_codec(fmt(idx, "ExtraFormat", "Video.Compression")),
                main_resolution=res(idx, "MainFormat"), sub_resolution=res(idx, "ExtraFormat")))
        return out

    async def snapshot(self, channel: int, stream: Literal["main", "sub"] = "main") -> bytes:
        resp = await self.http.get("/cgi-bin/snapshot.cgi", params=[("channel", str(int(channel)))])
        if not resp.content.startswith(b"\xff\xd8"):
            raise DeviceProtocolError(f"El equipo {self.http.label} no devolvió una imagen JPEG")
        return resp.content

    async def aclose(self) -> None:
        await self.http.aclose()
