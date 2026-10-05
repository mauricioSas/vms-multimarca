"""Lectura tolerante del SDP que devuelve un DESCRIBE (RFC 8866) para saber el códec de cada pista.

Tolera lo que se ve en equipos reales (PLAN-V2 §3.3, prueba 4):
- saltos de línea solo LF (o mezclados con CRLF);
- pistas sin `a=rtpmap` (tipos de carga estáticos como 26 = JPEG, o dinámicos con `a=fmtp` que delatan
  el códec: `sprop-vps` → H.265, `sprop-parameter-sets`/`profile-level-id` → H.264);
- sin `sprop-*` (el códec sale igual del `rtpmap`);
- sin `a=control` o con control absoluto (`rtsp://…/trackID=1`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

# Tipos de carga estáticos (RFC 3551) que interesan.
STATIC_PAYLOADS: dict[int, tuple[str, str]] = {
    0: ("audio", "PCMU"), 8: ("audio", "PCMA"), 14: ("audio", "MPA"), 26: ("video", "JPEG"),
    32: ("video", "MPV"), 33: ("video", "MP2T"), 34: ("video", "H263"),
}

_VIDEO_NAMES = {"H264": "H.264", "H265": "H.265", "HEVC": "H.265", "JPEG": "MJPEG", "MJPEG": "MJPEG",
                "MP4V-ES": "MPEG-4", "H263": "H.263", "H263-1998": "H.263", "MPV": "MPEG-2"}


@dataclass
class SdpMedia:
    kind: str                          # video, audio, application…
    payload_types: list[int] = field(default_factory=list)
    codec: str = ""                    # nombre del rtpmap en mayúsculas («H264», «PCMA»…) o «» si no se sabe
    clock_rate: int | None = None
    control: str = ""                  # tal cual (relativo, absoluto o «*»)
    fmtp: str = ""

    @property
    def normalized_codec(self) -> str | None:
        """«H.264», «H.265», «MJPEG»… (solo vídeo); None si no se sabe."""
        if self.kind != "video" or not self.codec:
            return None
        return _VIDEO_NAMES.get(self.codec, self.codec)


@dataclass
class SdpInfo:
    media: list[SdpMedia] = field(default_factory=list)
    session_control: str = ""

    @property
    def codecs(self) -> list[str]:
        return [m.codec for m in self.media if m.codec]

    @property
    def video(self) -> SdpMedia | None:
        return next((m for m in self.media if m.kind == "video"), None)

    @property
    def video_codec(self) -> str | None:
        v = self.video
        return v.normalized_codec if v else None

    def control_url(self, media: SdpMedia, base_url: str) -> str:
        """URL para el SETUP de una pista: control absoluto, relativo a la base o la base misma."""
        ctrl = media.control or self.session_control
        if not ctrl or ctrl == "*":
            return base_url
        if ctrl.lower().startswith(("rtsp://", "rtsps://")):
            return ctrl
        base = base_url if base_url.endswith("/") else base_url + "/"
        return urljoin(base, ctrl)


def _codec_from_fmtp(fmtp: str) -> str:
    low = fmtp.lower()
    if "sprop-vps" in low:
        return "H265"
    if "sprop-parameter-sets" in low or "profile-level-id" in low or "packetization-mode" in low:
        return "H264"
    return ""


def parse_sdp(text: str) -> SdpInfo:
    info = SdpInfo()
    current: SdpMedia | None = None
    rtpmaps: dict[tuple[int, int], tuple[str, int | None]] = {}   # (índice de media, pt) → (códec, reloj)
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        key, value = line[0], line[2:]
        if key == "m":
            parts = value.split()
            pts = [int(p) for p in parts[3:] if p.isdigit()]
            current = SdpMedia(kind=parts[0].lower() if parts else "", payload_types=pts)
            info.media.append(current)
        elif key == "a":
            attr, _, rest = value.partition(":")
            attr = attr.lower()
            if attr == "control":
                if current is None:
                    info.session_control = rest.strip()
                else:
                    current.control = rest.strip()
            elif attr == "rtpmap" and current is not None:
                pt_s, _, enc = rest.strip().partition(" ")
                name, _, clock = enc.partition("/")
                if pt_s.isdigit():
                    clock_v = clock.split("/")[0]
                    rtpmaps[(len(info.media) - 1, int(pt_s))] = (
                        name.strip().upper(), int(clock_v) if clock_v.isdigit() else None)
            elif attr == "fmtp" and current is not None:
                current.fmtp = rest.partition(" ")[2].strip() or rest.strip()
    for idx, m in enumerate(info.media):
        for pt in m.payload_types:
            if (idx, pt) in rtpmaps:
                m.codec, m.clock_rate = rtpmaps[(idx, pt)]
                break
            if pt in STATIC_PAYLOADS:
                kind, codec = STATIC_PAYLOADS[pt]
                if kind == m.kind or not m.kind:
                    m.codec = codec
                    m.clock_rate = 90000 if kind == "video" else 8000
                    break
        if not m.codec and m.kind == "video":
            m.codec = _codec_from_fmtp(m.fmtp)
    return info


__all__ = ["SdpInfo", "SdpMedia", "parse_sdp", "STATIC_PAYLOADS"]
