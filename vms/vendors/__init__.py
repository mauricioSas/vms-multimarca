"""Integración con fabricantes: registro de drivers, API de cada marca, prueba de conexión y descubrimiento.

Contrato: docs/CONTRATO.md §5.1 y §16 (las firmas de §5.1 no cambian):
    client_for(device, password, *, timeout=5.0) -> DeviceClient
    async test_device(device, password, *, timeout=5.0) -> DeviceTestResult   (nunca lanza)
    async discover(timeout=3.0, *, targets=None, interfaces=None) -> list[DiscoveredDevice]

Todo consulta el registro (`vms.vendors.registry`): no hay cadenas de `if` por marca.
"""
from __future__ import annotations

import logging

import httpx

from vms.core import rtsp
from vms.core.errors import DeviceAuthFailed, DeviceError, DeviceUnsupported
from vms.core.interfaces import Capability, ChannelInfo, DeviceClient, DeviceTestResult, DriverSpec
from vms.core.models import DeviceBase, DeviceKind

from .dahua import DahuaClient
from .discovery import discover, guess_vendor, parse_probe_matches
from .errors import BasicNotAllowed, DeviceLocked
from .hikvision import HikvisionClient
from .onvif_client import OnvifClient, rtsp_path_of
from .registry import REGISTRY, best_match, get_driver, preset_for, variants_for
from .rtsp_probe import SLOW_SECONDS, RtspProbeResult, probe_rtsp, tcp_reachable, tcp_state

log = logging.getLogger("vms.vendors")

__all__ = ["client_for", "test_device", "discover", "guess_vendor", "parse_probe_matches", "probe_rtsp",
           "tcp_reachable", "RtspProbeResult", "HikvisionClient", "DahuaClient", "OnvifClient", "rtsp_path_of",
           "REGISTRY", "best_match", "get_driver", "onvif_port_for", "has_api"]

KIND_ES = {"nvr": "Grabador", "dvr": "Grabador DVR", "xvr": "Grabador XVR", "camera": "Cámara"}


def onvif_port_for(device: DeviceBase, spec: DriverSpec | None) -> int:
    """Puerto ONVIF: el del equipo; si no tiene, el habitual del driver (Tapo 2020); si no, el HTTP."""
    if device.onvif_port:
        return device.onvif_port
    if spec is not None and spec.id != "onvif" and "onvif" in spec.default_ports:
        return spec.default_ports["onvif"]
    return device.http_port


def has_api(spec: DriverSpec | None) -> bool:
    return spec is not None and (spec.client is not None or Capability.ONVIF in spec.capabilities)


def _uses_onvif(spec: DriverSpec) -> bool:
    return spec.client is OnvifClient or (spec.client is None and Capability.ONVIF in spec.capabilities)


def client_for(device: DeviceBase, password: str, *, timeout: float = 5.0,
               transport: httpx.AsyncBaseTransport | None = None) -> DeviceClient:
    """Cliente de la API del equipo según su driver. `transport` solo para pruebas (mocks y fixtures sin red).

    Perfiles RTSP con ONVIF (Uniview, VIGI, Tapo…) se leen por ONVIF; los que no tienen ni API ni ONVIF
    (Ezviz, Reolink, RTSP manual) no tienen cliente."""
    spec = get_driver(device.vendor)
    if spec is None:
        raise DeviceUnsupported(f"La marca «{device.vendor}» no está disponible en esta versión del programa")
    if _uses_onvif(spec):
        return OnvifClient(device, password, timeout=timeout, transport=transport,
                           port=onvif_port_for(device, spec))
    if spec.client is not None:
        client: DeviceClient = spec.client(device, password, timeout=timeout, transport=transport)
        return client
    if spec.id == "generic":
        raise DeviceUnsupported("Los equipos «Genérico (RTSP manual)» no tienen API: indica la ruta RTSP a mano")
    raise DeviceUnsupported(f"Los equipos «{spec.name}» no tienen API: el alta usa solo RTSP")


def _rtsp_target(device: DeviceBase, spec: DriverSpec, channels: list[ChannelInfo], kind: DeviceKind
                 ) -> tuple[int, str, tuple[str, ...]] | None:
    """(canal, ruta principal, rutas alternativas) que usar en la prueba (el primer canal en línea si se sabe).

    Con preset de marca se usa el preset (es lo que se guardará); sin preset (ONVIF, Ajax, RTSP manual), la
    ruta que dio ONVIF."""
    ordered = sorted(channels, key=lambda c: (c.online is False, c.channel))
    if spec.presets is None:
        c = next((c for c in ordered if c.main_path), None)
        return (c.channel, c.main_path, ()) if c is not None and c.main_path else None
    ch = ordered[0].channel if ordered else 1
    preset = preset_for(device.vendor, ch, kind)
    if preset is None:
        return None
    return ch, preset.main, tuple(v.main for v in variants_for(device.vendor, ch, kind))


def _codec_warnings(spec: DriverSpec | None, channels: list[ChannelInfo], probe: RtspProbeResult | None) -> list[str]:
    out: list[str] = []
    can_fix = spec is not None and Capability.API_CODEC_FIX in spec.capabilities
    bad = [c for c in channels if c.has_sub and c.sub_codec and c.sub_codec != "H.264"]
    if bad:
        names = ", ".join(str(c.channel) for c in bad[:8]) + ("…" if len(bad) > 8 else "")
        codec = bad[0].sub_codec
        out.append(f"El subflujo de {'los canales' if len(bad) > 1 else 'el canal'} {names} va en {codec}: el "
                   "navegador no lo reproduce por WebRTC. " + ("Usa «Corregir códec» para ponerlo en H.264."
                                                               if can_fix else "Cámbialo a H.264 en el equipo."))
    if probe is not None and probe.ok and not channels:
        vc = probe.video_codec
        if vc == "MJPEG":
            out.append("El vídeo va en MJPEG: se graba, pero el muro no puede mostrarlo por WebRTC. Usa H.264.")
        elif vc == "H.265":
            out.append("El flujo probado va en H.265: para verlo en vivo, pon el subflujo en H.264.")
    if any(c.main_codec == "H.265" for c in channels):
        out.append("El flujo principal en H.265 se graba bien; a pantalla completa se verá el subflujo si el PC no "
                   "decodifica H.265.")
    return out


async def test_device(device: DeviceBase, password: str, *, timeout: float = 5.0,
                      transport: httpx.AsyncBaseTransport | None = None,
                      first_frame: bool = True) -> DeviceTestResult:
    """Prueba de conexión completa. Nunca lanza: el diagnóstico va en el resultado (en español).

    **Un solo intento con credenciales:** si la API rechaza la contraseña (o dice que el usuario está
    bloqueado) no se prueba RTSP; si la API no rechaza nada, RTSP manda las credenciales una vez."""
    result = DeviceTestResult(ok=False)
    host = device.host
    spec = get_driver(device.vendor)
    try:
        if spec is None:
            result.message = (f"La marca «{device.vendor}» no está disponible en esta versión del programa: "
                              "actualiza el programa o da de alta el equipo como ONVIF o RTSP manual.")
            return result
        api = has_api(spec)
        api_required = spec.client is not None          # perfiles: ONVIF es opcional (se prueba si responde)
        api_port = onvif_port_for(device, spec) if _uses_onvif(spec) else device.http_port
        http_ok = api and (transport is not None or await tcp_reachable(host, api_port, min(timeout, 3.0)))
        rtsp_state = await tcp_state(host, device.rtsp_port, min(timeout, 3.0))
        rtsp_reach = rtsp_state == "open"
        result.reachable = bool(http_ok or rtsp_reach)
        if not result.reachable:
            ports = f"HTTP {api_port} ni RTSP {device.rtsp_port}" if api else f"RTSP {device.rtsp_port}"
            result.message = (f"No se puede conectar con {host}: no responden los puertos {ports}. "
                              "Comprueba la IP, que el equipo esté encendido y que estés en la misma red o VPN.")
            return result

        notes: list[str] = []
        api_error = ""
        if api and http_ok:
            client = client_for(device, password, timeout=timeout, transport=transport)
            try:
                result.info = await client.probe()
                result.channels = await client.list_channels()
                result.auth_ok = True
            except DeviceLocked as exc:
                result.auth_ok, result.locked, result.lockout_minutes = False, True, exc.minutes
                result.message = exc.message
                return result  # bloqueado: ni un intento más
            except BasicNotAllowed as exc:
                result.auth_ok = None
                result.message = exc.message
                result.warnings.append(exc.message)
                return result  # no se manda la contraseña en claro; RTSP probaría lo mismo
            except DeviceAuthFailed as exc:
                result.auth_ok = False
                result.message = exc.message
                return result  # no seguimos con RTSP: más intentos fallidos pueden bloquear el usuario
            except DeviceError as exc:
                api_error = exc.message
                if api_required:
                    notes.append(f"La API del equipo falló: {exc.message}")
                else:
                    notes.append("ONVIF no responde (no es imprescindible): se prueba solo el vídeo RTSP.")
                    log.info("ONVIF opcional de %s sin respuesta: %s", host, exc.message)
            finally:
                await client.aclose()
        elif api and api_required:
            notes.append(f"El puerto HTTP {api_port} no responde; no se pudo leer modelo ni canales.")

        kind: DeviceKind = device.kind
        if result.info is not None and result.info.kind != "unknown":
            kind = result.info.kind
        target = _rtsp_target(device, spec, result.channels, kind)
        probe: RtspProbeResult | None = None
        if not rtsp_reach:
            result.rtsp_ok = False
            if rtsp_state == "refused":
                notes.append(f"El equipo rechaza la conexión en el puerto RTSP {device.rtsp_port}: RTSP está "
                             "desactivado o en otro puerto.")
            else:
                notes.append(f"El puerto RTSP {device.rtsp_port} no responde: sin él no hay vídeo.")
            old_fw = str(getattr(device, "firmware", "") or "")
            if result.info is not None and old_fw and result.info.firmware and result.info.firmware != old_fw:
                result.firmware_changed = f"{old_fw} → {result.info.firmware}"
                hints = " ".join(spec.setup_hints_es[:2])
                notes.append(f"El equipo se actualizó (firmware {old_fw} → {result.info.firmware}) y puede haber "
                             f"desactivado RTSP u ONVIF. {hints}".strip())
        elif target is None:
            notes.append("El puerto RTSP responde, pero hace falta indicar la ruta RTSP de cada cámara.")
        else:
            ch, path, alts = target
            probe = await probe_rtsp(host, device.rtsp_port, path, device.username, password, timeout=timeout,
                                     allow_basic=device.allow_basic, alt_paths=alts, first_frame=first_frame)
            result.rtsp_ok = probe.ok
            result.sdp_ms, result.first_frame_ms = probe.sdp_ms, probe.first_frame_ms
            if probe.ok:
                codec = probe.video_codec
                notes.append(f"Vídeo RTSP correcto en el canal {ch}" + (f" ({codec})." if codec else "."))
                if probe.path and probe.path != rtsp.normalize_path(path):
                    result.working_path = probe.path
                    notes.append(f"La ruta que funciona es {probe.path}: se usará esa.")
                    for c in result.channels:
                        if c.channel == ch:
                            c.main_path = probe.path
                if result.auth_ok is None and probe.auth_ok:
                    result.auth_ok = True
            else:
                if probe.locked:
                    result.locked, result.lockout_minutes = True, probe.lockout_minutes
                if probe.auth_ok is False:
                    result.auth_ok = False
                if probe.basic_only:
                    result.warnings.append(probe.error)
                result.session_limit = probe.session_limit
                notes.append(f"Prueba RTSP del canal {ch}: {probe.error or 'sin vídeo'}.")

        # avisos: códec, GOP largo, Basic, madurez y ancho de banda
        result.warnings.extend(_codec_warnings(spec, result.channels, probe))
        smart = [c for c in result.channels if c.smart_codec or (c.gop_seconds or 0) > SLOW_SECONDS]
        if (probe is not None and probe.slow) or smart:
            result.gop_slow = True
            result.warnings.append("El vídeo tarda en arrancar (más de 4 s): suele ser H.264+/H.265+ o un GOP largo. "
                                   "El muro esperará hasta 12 s; para arrancar antes, desactívalos en el subflujo.")
        if probe is not None and probe.auth_scheme == "basic":
            result.warnings.append("El equipo usa autenticación Basic: la contraseña viaja sin cifrar por la red.")
        if spec.maturity == "experimental":
            result.warnings.append(f"El driver de {spec.name} es experimental: se ha escrito con documentación pública "
                                   "y no se ha probado con un equipo real.")
        total = sum((c.main_bitrate_kbps or 0) + ((c.sub_bitrate_kbps or 0) if c.has_sub else 0)
                    for c in result.channels if c.online is not False)
        if total:
            result.bandwidth_kbps = total
            if len(result.channels) > 1:
                notes.append(f"Entrada estimada si importas todos los canales: {total / 1000:.1f} Mbit/s.")
        if result.session_limit:
            notes.append("El grabador no admite más sesiones: conecta las cámaras directamente o baja la calidad. "
                         "No se reintentará en bucle.")

        if result.auth_ok is False:
            result.ok = False
        elif api_required:
            result.ok = result.info is not None and result.rtsp_ok is not False
        else:
            result.ok = bool(result.rtsp_ok) or (rtsp_reach and target is None)
        head = ""
        if result.info is not None:
            kind_es = KIND_ES.get(result.info.kind, "Equipo")
            multi = result.info.kind in ("nvr", "dvr", "xvr")
            head = (f"{kind_es} {result.info.model or spec.name} encontrado"
                    + (f" con {len(result.channels)} canales." if multi else "."))
        elif api_error and api_required:
            head = "El equipo responde, pero su API dio error."
        result.message = " ".join(x for x in [head, *notes] if x).strip() or "Conexión correcta."
        return result
    except Exception as exc:  # noqa: BLE001 - la prueba nunca debe romper la petición
        log.exception("Error inesperado probando el equipo %s", host)
        result.message = f"Error inesperado al probar el equipo: {type(exc).__name__}"
        return result


test_device.__test__ = False  # type: ignore[attr-defined]  # que pytest no lo tome por una prueba
