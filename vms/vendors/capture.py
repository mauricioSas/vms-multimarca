"""Captura de respuestas reales de un equipo para las fixtures de la batería de contrato (PLAN-V2 §3.3).

Va dentro del programa (el instalador o Covert pueden capturar sus equipos sin instalar nada):
`python -m tools.capture_device --host 192.168.1.64 --driver hikvision --user admin --out tests/vendors/fixtures/hikvision/`

Garantías:
- **Solo lecturas.** El transporte que graba rechaza cualquier petición que no sea GET o una operación SOAP
  `Get*`, y cualquier GET de CGI con `action=set…`. En RTSP solo OPTIONS y DESCRIBE.
- **Una sola contraseña, una vez:** se pide con `getpass` (en la CLI), no se guarda, no se registra y nunca se
  graba la cabecera `Authorization`. Si el equipo la rechaza, se para.
- **Sin imágenes (RGPD):** no se piden capturas JPEG; la reproducción sirve una imagen sintética.
- **Anonimizada:** serie, MAC, IP, nombres de equipo y de canal, `realm`, `nonce` y `opaque` se sustituyen por
  valores deterministas (la misma entrada da la misma salida, así las fixtures no cambian en cada captura).

Estructura (`<out>/<modelo>__<firmware>/`): `meta.json`, `http/NNNN.json` (petición y respuesta, también las
SOAP de ONVIF con su operación), `rtsp/handshake.json`, `rtsp/main_ch1.sdp` y `discovery/hints.json`.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import ipaddress
import json
import logging
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from vms.core.errors import DeviceAuthFailed, DeviceError
from vms.core.interfaces import Capability, ChannelInfo, DetectionHints, DeviceInfo
from vms.core.models import DeviceBase

from .detect import scope_values
from .registry import REGISTRY, get_driver, preset_for, variants_for

log = logging.getLogger("vms.vendors.capture")

_SOAP_OP_RE = re.compile(r"<(?:[\w.\-]+:)?Body[^>]*>\s*<(?:[\w.\-]+:)?(\w+)")
_IPV4_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
_MAC_RE = re.compile(r"(?i)\b([0-9a-f]{2})([:-])([0-9a-f]{2})\2([0-9a-f]{2})\2([0-9a-f]{2})\2([0-9a-f]{2})\2([0-9a-f]{2})\b")
_PARAM_RE = re.compile(r'(realm|nonce|opaque)="([^"]*)"', re.IGNORECASE)
KEEP_IPS = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "255.255.255.0", "255.255.0.0", "255.0.0.0"}
DROP_HEADERS = {"set-cookie", "date", "authorization", "etag", "last-modified", "x-frame-options",
                "content-length", "cseq", "session", "content-base", "content-location"}
SYNTHETIC_JPEG_NOTE = "omitido: la captura nunca guarda imágenes (RGPD)"


class CaptureRefused(RuntimeError):
    """La captura intentó algo que no es una lectura: se corta antes de enviarlo."""


def soap_operation(body: bytes | str) -> str:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    m = _SOAP_OP_RE.search(text)
    return m.group(1) if m else ""


@dataclass
class Exchange:
    method: str
    path: str
    query: list[tuple[str, str]]
    soap_op: str
    authorized: bool
    status: int
    headers: dict[str, list[str]]
    body: str
    binary: bool = False

    def as_json(self) -> dict[str, Any]:
        return {"request": {"method": self.method, "path": self.path, "query": self.query, "soap_op": self.soap_op,
                            "authorized": self.authorized},
                "response": {"status": self.status, "headers": self.headers, "body": self.body,
                             "binary": self.binary}}


class RecordingTransport(httpx.AsyncBaseTransport):
    """Transporte httpx que deja pasar solo lecturas y graba cada intercambio (sin credenciales)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.exchanges: list[Exchange] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content if request.method == "POST" else b""
        op = soap_operation(body) if body else ""
        query = [(k, v) for k, v in parse_qsl(request.url.query.decode("ascii", errors="replace"), keep_blank_values=True)]
        if request.method not in ("GET", "POST"):
            raise CaptureRefused(f"La captura solo lee: se ha bloqueado un {request.method}")
        if request.method == "POST" and not op.startswith("Get"):
            raise CaptureRefused(f"La captura solo lee: se ha bloqueado la operación SOAP {op or '(desconocida)'}")
        if any(k.lower() == "action" and v.lower().startswith(("set", "reboot", "delete", "add", "remove", "factory"))
               for k, v in query):
            raise CaptureRefused("La captura solo lee: se ha bloqueado una acción de escritura")
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        ctype = response.headers.get("content-type", "")
        binary = ctype.startswith("image/") or content.startswith(b"\xff\xd8")
        headers: dict[str, list[str]] = {}
        for k, v in response.headers.multi_items():
            if k.lower() not in DROP_HEADERS:
                headers.setdefault(k.lower(), []).append(v)
        self.exchanges.append(Exchange(
            method=request.method, path=request.url.path, query=query, soap_op=op,
            authorized="authorization" in request.headers or b"UsernameToken" in body,
            status=response.status_code, headers=headers,
            body=SYNTHETIC_JPEG_NOTE if binary else content.decode("utf-8", errors="replace"), binary=binary))
        return httpx.Response(response.status_code, headers=response.headers, content=content,
                              extensions=response.extensions)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class Anonymizer:
    """Sustituciones deterministas: el mismo valor de entrada da siempre el mismo valor anonimizado."""

    literal: dict[str, str] = field(default_factory=dict)
    ips: dict[str, str] = field(default_factory=dict)
    macs: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _tag(value: str, n: int = 8) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n].upper()

    def add_serial(self, serial: str, model: str = "") -> str:
        if not serial:
            return ""
        prefix = model if model and serial.startswith(model) else ""
        anon = f"{prefix}SN{self._tag(serial, 10)}"
        self.literal[serial] = anon
        return anon

    def add_name(self, value: str, replacement: str) -> None:
        if value and len(value) >= 3 and value not in self.literal:
            self.literal[value] = replacement

    def ip(self, value: str) -> str:
        try:
            ip = ipaddress.IPv4Address(value)
        except ValueError:
            return value
        if value in KEEP_IPS or ip.is_multicast or value.startswith("255."):
            return value
        if value not in self.ips:
            self.ips[value] = f"192.0.2.{len(self.ips) + 10}"
        return self.ips[value]

    def mac(self, m: re.Match[str]) -> str:
        raw = "".join(m.group(i) for i in (1, 3, 4, 5, 6, 7)).lower()
        if raw not in self.macs:
            n = len(self.macs) + 1
            self.macs[raw] = f"02:00:5e:00:53:{n:02x}"
        out = self.macs[raw]
        return out.replace(":", m.group(2)) if m.group(2) == "-" else out

    def text(self, value: str) -> str:
        if not value:
            return value
        for src in sorted(self.literal, key=len, reverse=True):
            value = value.replace(src, self.literal[src])
        value = _MAC_RE.sub(self.mac, value)
        value = _IPV4_RE.sub(lambda m: self.ip(m.group(1)), value)
        return value

    def challenge(self, header: str, driver: str) -> str:
        counter = iter(range(1, 100))

        def repl(m: re.Match[str]) -> str:
            key = m.group(1).lower()
            if key == "realm":
                return f'{m.group(1)}="{driver}-realm"'
            return f'{m.group(1)}="{key}-anon-{next(counter)}"'
        return _PARAM_RE.sub(repl, header)


def _path_only(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "desconocido"


async def capture(device: DeviceBase, password: str, out_dir: Path, *, transport: httpx.AsyncBaseTransport | None = None,
                  timeout: float = 5.0, source: str = "captura", notes: str = "", synthetic: bool = False,
                  hints: DetectionHints | None = None, discover_timeout: float = 1.5,
                  record_failure: bool = False) -> Path:
    """Captura (solo lecturas) y escribe la carpeta de fixtures. Devuelve su ruta.

    `record_failure` (solo para fixtures sintéticas contra los simuladores; la CLI no lo ofrece): graba además
    la respuesta a una contraseña mala para que la batería compruebe «contraseña mala ≠ bloqueo»."""
    from . import client_for, has_api
    from .rtsp_probe import probe_rtsp

    spec = get_driver(device.vendor)
    if spec is None:
        raise ValueError(f"Driver desconocido: {device.vendor}")
    recorder = RecordingTransport(transport or httpx.AsyncHTTPTransport(verify=False))
    info: DeviceInfo | None = None
    channels: list[ChannelInfo] = []
    if has_api(spec):
        client = client_for(device, password, timeout=timeout, transport=recorder)
        try:
            info = await client.probe()
            channels = await client.list_channels()
            snap_uri = getattr(client, "snapshot_uri", None)
            if channels and snap_uri is not None:       # ONVIF: solo la URI, nunca la imagen
                for stream in ("main", "sub"):
                    try:
                        await snap_uri(channels[0].channel, stream)
                    except DeviceError as exc:
                        log.info("Sin URI de imagen: %s", exc.message)
            if Capability.TIME_READ in spec.capabilities and hasattr(client, "device_time"):
                try:
                    await client.device_time()
                except DeviceError as exc:
                    log.info("Sin hora del equipo: %s", exc.message)
        except DeviceAuthFailed:
            raise
        except DeviceError as exc:
            if spec.client is not None:
                raise
            log.info("ONVIF opcional sin respuesta durante la captura: %s", exc.message)
        finally:
            await client.aclose()
    failure: Exchange | None = None
    if record_failure and has_api(spec) and recorder.exchanges:
        rec2 = RecordingTransport(transport or httpx.AsyncHTTPTransport(verify=False))
        bad = client_for(device, password + "-mala", timeout=timeout, transport=rec2)
        try:
            await bad.probe()
        except DeviceError:
            pass
        finally:
            await bad.aclose()
        failure = next((e for e in rec2.exchanges if e.authorized and e.status in (400, 401, 403)), None)

    kind = info.kind if info is not None and info.kind != "unknown" else device.kind
    first = sorted(channels, key=lambda c: (c.online is False, c.channel))[0] if channels else None
    ch = first.channel if first else 1
    preset = preset_for(device.vendor, ch, kind)
    rtsp_path = preset.main if preset else (first.main_path if first and first.main_path else "")
    trace: list[dict[str, object]] = []
    sdp_text = ""
    codec = None
    if rtsp_path:
        alts = tuple(v.main for v in variants_for(device.vendor, ch, kind)) if preset else ()
        probe = await probe_rtsp(device.host, device.rtsp_port, rtsp_path, device.username, password,
                                 timeout=timeout, allow_basic=device.allow_basic, trace=trace, alt_paths=alts)
        sdp_text, codec = probe.sdp_text, probe.video_codec
        if probe.ok and probe.path:
            rtsp_path = probe.path          # la ruta que respondió (puede ser una variante del driver)
    if hints is None:
        hints = await _discover_hints(device.host, discover_timeout)

    anon = Anonymizer()
    model = info.model if info else (hints.model or next(iter(scope_values(hints.scopes, "hardware")), "").strip())
    serial_anon = anon.add_serial(info.serial, model) if info else ""
    if info is not None:
        anon.add_name(info.name, "Equipo")
        if info.mac:
            anon.text(info.mac)
    for c in channels:
        anon.add_name(c.name, f"Canal {c.channel}")
    anon.add_name(device.name, "Equipo")
    firmware = info.firmware if info else ""
    folder = Path(out_dir) / f"{_safe(model or spec.id)}__{_safe(firmware or 'sin-firmware')}"
    for sub in ("http", "rtsp", "discovery"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    for old in (folder / "http").glob("*.json"):
        old.unlink()

    def clean(e: Exchange) -> dict[str, Any]:
        data = e.as_json()
        resp = data["response"]
        resp["body"] = anon.text(resp["body"]) if not e.binary else resp["body"]
        resp["headers"] = {k: [anon.challenge(anon.text(v), spec.id) if k == "www-authenticate" else anon.text(v)
                               for v in vs] for k, vs in resp["headers"].items()}
        data["request"]["query"] = [[k, anon.text(v)] for k, v in e.query]
        data["request"]["path"] = anon.text(e.path)
        return data

    for i, e in enumerate(recorder.exchanges, start=1):
        _write_json(folder / "http" / f"{i:04d}.json", clean(e))
    if failure is not None:
        _write_json(folder / "http" / "auth_failure.json", clean(failure))
    handshake = [{**t, "url": _path_only(str(t["url"])), "body": anon.text(str(t["body"])),
                  "headers": {k: [anon.challenge(anon.text(v), spec.id) if k == "www-authenticate" else anon.text(v)
                                  for v in vs] for k, vs in dict(t["headers"]).items()  # type: ignore[call-overload]
                              if k not in DROP_HEADERS}}
                 for t in trace]
    _write_json(folder / "rtsp" / "handshake.json", {"path": rtsp_path, "steps": handshake})
    if sdp_text:
        (folder / "rtsp" / "main_ch1.sdp").write_text(anon.text(sdp_text), encoding="utf-8")
    _write_json(folder / "discovery" / "hints.json", json.loads(anon.text(hints.model_dump_json())))
    meta = {"driver": spec.id, "model": model, "firmware": firmware,
            "firmware_date": info.firmware_date if info else "", "kind": kind,
            "captured": date.today().isoformat(), "source": source, "anonymized": True, "synthetic": synthetic,
            "serial": serial_anon, "channels": len(channels) if channels else (1 if rtsp_path else 0),
            "channel_ids": [c.channel for c in channels], "main_codec": codec, "rtsp_path": rtsp_path,
            "snapshot": "sintético (la captura no guarda imágenes)",
            "notes": notes}
    _write_json(folder / "meta.json", meta)
    log.info("Fixtures escritas en %s (%d intercambios HTTP)", folder, len(recorder.exchanges))
    return folder


async def _discover_hints(host: str, timeout: float) -> DetectionHints:
    from .discovery import discover

    try:
        found = await discover(timeout, targets=[(host, 3702)], sadp_targets=[(host, 37020)],
                               dhip_targets=[(host, 37810)])
    except OSError as exc:
        log.info("Descubrimiento no disponible durante la captura: %s", exc)
        found = []
    d = next((x for x in found if x.host == host), None)
    if d is None:
        return DetectionHints()
    return DetectionHints(scopes=d.scopes, model=d.model, name=d.name, mac=d.mac, sadp="sadp" in d.sources,
                          dhip="dhip" in d.sources, manufacturer=d.name if "dhip" in d.sources else "")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def iter_fixture_dirs(root: Path) -> Iterable[Path]:
    for meta in sorted(Path(root).glob("*/*/meta.json")):
        yield meta.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m vms.vendors.capture",
                                 description="Captura respuestas de un equipo (solo lecturas, anonimizadas) para fixtures.")
    ap.add_argument("--host", required=True, help="IP o nombre del equipo")
    ap.add_argument("--driver", required=True, choices=sorted(REGISTRY), help="driver del equipo")
    ap.add_argument("--user", default="admin", help="usuario (mejor uno de solo lectura)")
    ap.add_argument("--kind", default="camera", choices=["camera", "nvr", "dvr", "xvr"])
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--rtsp-port", type=int, default=None, help="por defecto, el habitual del driver")
    ap.add_argument("--onvif-port", type=int, default=None)
    ap.add_argument("--https", action="store_true")
    ap.add_argument("--allow-basic", action="store_true", help="permitir Basic si el equipo no admite Digest")
    ap.add_argument("--out", type=Path, default=None, help="carpeta de salida (se crea <modelo>__<firmware>/ dentro)")
    ap.add_argument("--source", default="captura", help="quién/dónde se capturó (p. ej. «laboratorio», «Covert»)")
    ap.add_argument("--notes", default="", help="notas (canales sin vídeo, H.265…)")
    ap.add_argument("--timeout", type=float, default=5.0)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    spec = REGISTRY[args.driver]
    password = getpass.getpass(f"Contraseña de {args.user}@{args.host} (no se guarda): ")
    device = DeviceBase(name="captura", vendor=args.driver, kind=args.kind, host=args.host, http_port=args.http_port,
                        rtsp_port=args.rtsp_port or spec.default_ports.get("rtsp", 554), onvif_port=args.onvif_port,
                        https=args.https, username=args.user, allow_basic=args.allow_basic)
    out = args.out or Path("capturas") / args.driver
    try:
        folder = asyncio.run(capture(device, password, out, timeout=args.timeout, source=args.source,
                                     notes=args.notes))
    except CaptureRefused as exc:
        print(f"Captura detenida: {exc}", file=sys.stderr)
        return 3
    except DeviceError as exc:
        print(f"No se pudo capturar: {exc.message}", file=sys.stderr)
        return 2
    print(f"Fixtures escritas en {folder}. Revísalas antes de subirlas (no deben contener datos del cliente).")
    return 0



__all__ = ["main", "parse_args", "Anonymizer", "CaptureRefused", "Exchange", "RecordingTransport", "capture", "iter_fixture_dirs",
           "soap_operation"]


if __name__ == "__main__":
    raise SystemExit(main())
