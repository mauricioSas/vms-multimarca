"""Integración con fabricantes: Hikvision ISAPI, Dahua CGI, ONVIF y descubrimiento en red.

Contrato: docs/CONTRATO.md §5.1.
    client_for(device, password, *, timeout=5.0) -> DeviceClient
    async test_device(device, password, *, timeout=5.0) -> DeviceTestResult   (nunca lanza)
    async discover(timeout=3.0, *, targets=None, interfaces=None) -> list[DiscoveredDevice]
"""
from __future__ import annotations

import logging

import httpx

from vms.core import rtsp
from vms.core.errors import DeviceAuthFailed, DeviceError, DeviceUnsupported
from vms.core.interfaces import ChannelInfo, DeviceClient, DeviceTestResult
from vms.core.models import DeviceBase

from .dahua import DahuaClient
from .discovery import discover, guess_vendor, parse_probe_matches
from .hikvision import HikvisionClient
from .onvif_client import OnvifClient, rtsp_path_of
from .rtsp_probe import RtspProbeResult, probe_rtsp, tcp_reachable

log = logging.getLogger("vms.vendors")

__all__ = ["client_for", "test_device", "discover", "guess_vendor", "parse_probe_matches", "probe_rtsp",
           "tcp_reachable", "RtspProbeResult", "HikvisionClient", "DahuaClient", "OnvifClient", "rtsp_path_of"]


def client_for(device: DeviceBase, password: str, *, timeout: float = 5.0,
               transport: httpx.AsyncBaseTransport | None = None) -> DeviceClient:
    """Cliente de la API del fabricante. `transport` solo para pruebas (mocks ASGI sin red)."""
    if device.vendor == "hikvision":
        return HikvisionClient(device, password, timeout=timeout, transport=transport)
    if device.vendor == "dahua":
        return DahuaClient(device, password, timeout=timeout, transport=transport)
    if device.vendor == "onvif":
        return OnvifClient(device, password, timeout=timeout)
    raise DeviceUnsupported("Los equipos «Genérico (RTSP manual)» no tienen API: indica la ruta RTSP a mano")


def _first_stream_path(device: DeviceBase, channels: list[ChannelInfo]) -> tuple[int, str] | None:
    """Canal y ruta RTSP principal que usar en la prueba (el primero en línea si se sabe)."""
    ordered = sorted(channels, key=lambda c: (c.online is False, c.channel))
    if device.vendor == "onvif":
        for c in ordered:
            if c.main_path:
                return c.channel, c.main_path
        return None
    ch = ordered[0].channel if ordered else 1
    preset = rtsp.preset_paths(device.vendor, ch)
    return (ch, preset[0]) if preset else None


async def test_device(device: DeviceBase, password: str, *, timeout: float = 5.0,
                      transport: httpx.AsyncBaseTransport | None = None) -> DeviceTestResult:
    """Prueba de conexión completa. Nunca lanza: el diagnóstico va en el resultado (en español)."""
    result = DeviceTestResult(ok=False)
    host = device.host
    try:
        api_port = (device.onvif_port or device.http_port) if device.vendor == "onvif" else device.http_port
        has_api = device.vendor != "generic"
        http_ok = has_api and (transport is not None or await tcp_reachable(host, api_port, min(timeout, 3.0)))
        rtsp_reach = await tcp_reachable(host, device.rtsp_port, min(timeout, 3.0))
        result.reachable = bool(http_ok or rtsp_reach)
        if not result.reachable:
            ports = f"HTTP {api_port} ni RTSP {device.rtsp_port}" if has_api else f"RTSP {device.rtsp_port}"
            result.message = (f"No se puede conectar con {host}: no responden los puertos {ports}. "
                              "Comprueba la IP, que el equipo esté encendido y que estés en la misma red o VPN.")
            return result

        notes: list[str] = []
        api_error = ""
        if has_api and http_ok:
            client = client_for(device, password, timeout=timeout, transport=transport)
            try:
                result.info = await client.probe()
                result.channels = await client.list_channels()
                result.auth_ok = True
            except DeviceAuthFailed as exc:
                result.auth_ok = False
                result.message = exc.message
                return result  # no seguimos con RTSP: más intentos fallidos pueden bloquear el usuario
            except DeviceError as exc:
                api_error = exc.message
                notes.append(f"La API del equipo falló: {exc.message}")
            finally:
                await client.aclose()
        elif has_api:
            notes.append(f"El puerto HTTP {api_port} no responde; no se pudo leer modelo ni canales.")

        target = _first_stream_path(device, result.channels)
        if not rtsp_reach:
            result.rtsp_ok = False
            notes.append(f"El puerto RTSP {device.rtsp_port} no responde: sin él no hay vídeo.")
        elif target is None:
            notes.append("El puerto RTSP responde, pero hace falta indicar la ruta RTSP de cada cámara.")
        else:
            ch, path = target
            probe = await probe_rtsp(host, device.rtsp_port, path, device.username, password, timeout=timeout)
            result.rtsp_ok = probe.ok
            if probe.ok:
                codec = probe.video_codec
                notes.append(f"Vídeo RTSP correcto en el canal {ch}" + (f" ({codec})." if codec else "."))
                if result.auth_ok is None:
                    result.auth_ok = True
            else:
                if probe.auth_ok is False:
                    result.auth_ok = False
                notes.append(f"Prueba RTSP del canal {ch}: {probe.error or 'sin vídeo'}.")

        if result.auth_ok is False:
            result.ok = False
        elif has_api:
            result.ok = result.info is not None and result.rtsp_ok is not False
        else:
            result.ok = bool(result.rtsp_ok) or rtsp_reach
        head = ""
        if result.info is not None:
            kind = {"nvr": "Grabador", "camera": "Cámara"}.get(result.info.kind, "Equipo")
            head = (f"{kind} {result.info.model or rtsp.VENDORS.get(device.vendor, '')} encontrado"
                    + (f" con {len(result.channels)} canales." if result.info.kind == "nvr" else "."))
        elif api_error:
            head = "El equipo responde, pero su API dio error."
        result.message = " ".join(x for x in [head, *notes] if x).strip() or "Conexión correcta."
        return result
    except Exception as exc:  # noqa: BLE001 - la prueba nunca debe romper la petición
        log.exception("Error inesperado probando el equipo %s", host)
        result.message = f"Error inesperado al probar el equipo: {type(exc).__name__}"
        return result


test_device.__test__ = False  # type: ignore[attr-defined]  # que pytest no lo tome por una prueba
