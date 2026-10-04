"""Mock ONVIF (Device + Media ver10) con WS-Security UsernameToken (PasswordDigest).

Operaciones: GetSystemDateAndTime (sin autenticar), GetServices, GetCapabilities,
GetDeviceInformation, GetProfiles, GetStreamUri, GetSnapshotUri. Respuestas SOAP 1.2 con
los espacios de nombres reales, compatibles con onvif-zeep-async.

Las URIs que devuelve se construyen con `base_url` (el host:puerto donde se sirve el mock)
y `rtsp_base` (p. ej. un equipo del simulador de cámaras), así la prueba puede encadenar
GetStreamUri → MediaMTX → vídeo real.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

SNAPSHOT = (Path(__file__).parent / "assets" / "snapshot.jpg").read_bytes()
SOAP_CT = "application/soap+xml; charset=utf-8"
NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
      'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
      'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
      'xmlns:tt="http://www.onvif.org/ver10/schema" '
      'xmlns:ter="http://www.onvif.org/ver10/error"')


def _envelope(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<s:Envelope {NS}><s:Body>{body}</s:Body></s:Envelope>'


def _fault(subcode: str, text: str) -> str:
    return _envelope(
        "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>" + subcode +
        "</s:Value></s:Subcode></s:Code><s:Reason><s:Text xml:lang=\"en\">" + escape(text) +
        "</s:Text></s:Reason></s:Fault>")


@dataclass
class OnvifProfile:
    token: str
    name: str
    width: int
    height: int
    rtsp_path: str
    encoding: str = "H264"


@dataclass
class OnvifMock:
    base_url: str = "http://127.0.0.1:8080"
    rtsp_base: str = "rtsp://127.0.0.1:554"
    username: str = "admin"
    password: str = "Onvif#Pass:1@"
    manufacturer: str = "HIKVISION"
    model: str = "DS-2CD2143G2-I"
    firmware: str = "V5.7.3 build 220112"
    serial: str = "DS-2CD2143G2-I20210315AAWRG12345678"
    hardware_id: str = "88"
    profiles: list[OnvifProfile] = field(default_factory=lambda: [
        OnvifProfile("Profile_1", "mainStream", 2560, 1440, "/Streaming/Channels/101"),
        OnvifProfile("Profile_2", "subStream", 640, 360, "/Streaming/Channels/102"),
    ])
    operations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.app = Starlette(routes=[
            Route("/onvif/device_service", self.device_service, methods=["POST"]),
            Route("/onvif/media_service", self.media_service, methods=["POST"]),
            Route("/onvif/snapshot", self.snapshot, methods=["GET"]),
        ])

    # ------------------------------------------------------------------ utilidades
    @staticmethod
    def _operation(xml: str) -> str:
        m = re.search(r"<(?:[\w.\-]+:)?Body[^>]*>\s*<(?:([\w.\-]+):)?(\w+)", xml)
        return m.group(2) if m else ""

    def _authorized(self, xml: str) -> bool:
        user = re.search(r"<(?:[\w.\-]+:)?Username[^>]*>([^<]*)</", xml)
        pwd = re.search(r"<(?:[\w.\-]+:)?Password[^>]*>([^<]*)</", xml)
        nonce = re.search(r"<(?:[\w.\-]+:)?Nonce[^>]*>([^<]*)</", xml)
        created = re.search(r"<(?:[\w.\-]+:)?Created[^>]*>([^<]*)</", xml)
        if not (user and pwd and nonce and created) or user.group(1) != self.username:
            return False
        try:
            raw_nonce = base64.b64decode(nonce.group(1))
        except ValueError:
            return False
        digest = base64.b64encode(hashlib.sha1(raw_nonce + created.group(1).encode() + self.password.encode())
                                  .digest()).decode()
        return hmac.compare_digest(digest, pwd.group(1))

    def _reply(self, body: str) -> Response:
        return Response(_envelope(body), media_type=SOAP_CT)

    def _not_authorized(self) -> Response:
        return Response(_fault("ter:NotAuthorized", "Sender not Authorized"), status_code=400, media_type=SOAP_CT)

    # ------------------------------------------------------------------ device
    async def device_service(self, request: Request) -> Response:
        xml = (await request.body()).decode("utf-8", errors="replace")
        op = self._operation(xml)
        self.operations.append(op)
        if op == "GetSystemDateAndTime":
            now = datetime.now(timezone.utc)
            return self._reply(
                "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
                "<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
                "<tt:TimeZone><tt:TZ>CET-1CEST,M3.5.0,M10.5.0/3</tt:TZ></tt:TimeZone>"
                f"<tt:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute>"
                f"<tt:Second>{now.second}</tt:Second></tt:Time><tt:Date><tt:Year>{now.year}</tt:Year>"
                f"<tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date></tt:UTCDateTime>"
                "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
        if not self._authorized(xml):
            return self._not_authorized()
        if op == "GetServices":
            def svc(ns: str, path: str) -> str:
                return (f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{self.base_url}{path}</tds:XAddr>"
                        "<tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>")
            return self._reply("<tds:GetServicesResponse>"
                               + svc("http://www.onvif.org/ver10/device/wsdl", "/onvif/device_service")
                               + svc("http://www.onvif.org/ver10/media/wsdl", "/onvif/media_service")
                               + "</tds:GetServicesResponse>")
        if op == "GetCapabilities":
            return self._reply(
                "<tds:GetCapabilitiesResponse><tds:Capabilities>"
                f"<tt:Device><tt:XAddr>{self.base_url}/onvif/device_service</tt:XAddr></tt:Device>"
                f"<tt:Media><tt:XAddr>{self.base_url}/onvif/media_service</tt:XAddr>"
                "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
                "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>"
                "</tds:Capabilities></tds:GetCapabilitiesResponse>")
        if op == "GetDeviceInformation":
            return self._reply(
                "<tds:GetDeviceInformationResponse>"
                f"<tds:Manufacturer>{escape(self.manufacturer)}</tds:Manufacturer><tds:Model>{escape(self.model)}</tds:Model>"
                f"<tds:FirmwareVersion>{escape(self.firmware)}</tds:FirmwareVersion>"
                f"<tds:SerialNumber>{escape(self.serial)}</tds:SerialNumber><tds:HardwareId>{self.hardware_id}</tds:HardwareId>"
                "</tds:GetDeviceInformationResponse>")
        return Response(_fault("ter:ActionNotSupported", f"{op} not supported"), status_code=400, media_type=SOAP_CT)

    # ------------------------------------------------------------------ media
    def _profile_xml(self, p: OnvifProfile) -> str:
        return (f'<trt:Profiles token="{p.token}" fixed="true"><tt:Name>{p.name}</tt:Name>'
                '<tt:VideoSourceConfiguration token="VideoSourceToken"><tt:Name>VideoSourceConfig</tt:Name>'
                "<tt:UseCount>2</tt:UseCount><tt:SourceToken>VideoSource_1</tt:SourceToken>"
                f'<tt:Bounds x="0" y="0" width="{self.profiles[0].width}" height="{self.profiles[0].height}"/>'
                "</tt:VideoSourceConfiguration>"
                f'<tt:VideoEncoderConfiguration token="VideoEncoder_{p.token}"><tt:Name>{p.name}</tt:Name>'
                f"<tt:UseCount>1</tt:UseCount><tt:Encoding>{p.encoding}</tt:Encoding>"
                f"<tt:Resolution><tt:Width>{p.width}</tt:Width><tt:Height>{p.height}</tt:Height></tt:Resolution>"
                "<tt:Quality>3</tt:Quality><tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit>"
                "<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>"
                "<tt:H264><tt:GovLength>50</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>"
                "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
                "<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
                "<tt:SessionTimeout>PT60S</tt:SessionTimeout></tt:VideoEncoderConfiguration></trt:Profiles>")

    def _find_profile(self, xml: str) -> OnvifProfile | None:
        m = re.search(r"<(?:[\w.\-]+:)?ProfileToken[^>]*>([^<]*)</", xml)
        token = m.group(1) if m else ""
        return next((p for p in self.profiles if p.token == token), None)

    async def media_service(self, request: Request) -> Response:
        xml = (await request.body()).decode("utf-8", errors="replace")
        op = self._operation(xml)
        self.operations.append(op)
        if not self._authorized(xml):
            return self._not_authorized()
        if op == "GetProfiles":
            return self._reply("<trt:GetProfilesResponse>" + "".join(self._profile_xml(p) for p in self.profiles)
                               + "</trt:GetProfilesResponse>")
        if op in ("GetStreamUri", "GetSnapshotUri"):
            p = self._find_profile(xml)
            if p is None:
                return Response(_fault("ter:InvalidArgVal", "No such profile"), status_code=400, media_type=SOAP_CT)
            uri = (f"{self.rtsp_base}{p.rtsp_path}" if op == "GetStreamUri"
                   else f"{self.base_url}/onvif/snapshot?profile={p.token}")
            tag = "GetStreamUriResponse" if op == "GetStreamUri" else "GetSnapshotUriResponse"
            return self._reply(f"<trt:{tag}><trt:MediaUri><tt:Uri>{escape(uri)}</tt:Uri>"
                               "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                               "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
                               f"<tt:Timeout>PT60S</tt:Timeout></trt:MediaUri></trt:{tag}>")
        return Response(_fault("ter:ActionNotSupported", f"{op} not supported"), status_code=400, media_type=SOAP_CT)

    async def snapshot(self, request: Request) -> Response:
        return Response(SNAPSHOT, media_type="image/jpeg")


def probe_match_xml(relates_to: str, *, address_uuid: str, xaddr: str, scopes: list[str],
                    types: str = "dn:NetworkVideoTransmitter tds:Device") -> str:
    """ProbeMatches de WS-Discovery tal y como lo envía una cámara real."""
    import uuid
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
            f"<SOAP-ENV:Header><wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"<wsa:RelatesTo>{escape(relates_to)}</wsa:RelatesTo>"
            "<wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>"
            "<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>"
            "</SOAP-ENV:Header><SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch>"
            f"<wsa:EndpointReference><wsa:Address>urn:uuid:{address_uuid}</wsa:Address></wsa:EndpointReference>"
            f"<d:Types>{types}</d:Types><d:Scopes>{escape(' '.join(scopes))}</d:Scopes>"
            f"<d:XAddrs>{escape(xaddr)}</d:XAddrs><d:MetadataVersion>10</d:MetadataVersion>"
            "</d:ProbeMatch></d:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>")


def hikvision_scopes(model: str = "DS-2CD2143G2-I", mac: str = "c4:2f:90:f1:e6:a1") -> list[str]:
    return ["onvif://www.onvif.org/type/video_encoder", "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/Profile/G", "onvif://www.onvif.org/Profile/T",
            f"onvif://www.onvif.org/MAC/{mac}", f"onvif://www.onvif.org/hardware/{model}",
            f"onvif://www.onvif.org/name/HIKVISION%20{model}", "onvif://www.onvif.org/location/city/hangzhou"]


def dahua_scopes(model: str = "IPC-HDW2431T-AS-S2") -> list[str]:
    return ["onvif://www.onvif.org/location/country/china", "onvif://www.onvif.org/name/Dahua",
            f"onvif://www.onvif.org/hardware/{model}", "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/type/Network_Video_Transmitter", "onvif://www.onvif.org/extension/unique_identifier"]
