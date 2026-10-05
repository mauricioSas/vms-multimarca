"""Auditoría de seguridad de los equipos dados de alta (CONTRATO §18.12).

Principios: solo equipos dados de alta por el integrador; **sin explotar nada ni probar contraseñas**.
- Contraseña guardada débil o de fábrica: análisis **local** de la que ya está guardada (ni una petición).
- Usuario `admin` en vez del de solo lectura.
- RTSP y ONVIF anónimos: UNA petición sin credenciales a cada uno.
- Servicios: conexión TCP con tiempo límite a 23 (Telnet), 22 (SSH), 80/443 (web sin TLS), 8000/37777
  (SDK), y una búsqueda SSDP pasiva desde el PC (UPnP). Con credenciales de administrador TEMPORALES
  (opcional, nunca se guardan ni se registran) y un driver con `security_read`: Telnet, UPnP, P2P y más.
- Firmware con CVE según la tabla de avisos propia; hora (última medida).
Resultado por comprobación: vulnerable, probablemente vulnerable, sin CVE conocidos en la tabla («ok») o
desconocido. Nunca «seguro».
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from vms.core.errors import DeviceError
from vms.core.interfaces import DeviceClient, DeviceSecurityClient, DeviceSecuritySettings
from vms.core.models import Camera, Device
from vms.core.rtsp import format_host, preset_paths, redact
from vms.core.sources import camera_paths

from ..drivers import SECURITY_READ, brand_from, capabilities, driver_name, has_api
from ..host import dynamic
from ..models import ClockCheck, SecurityAuditReport, SecurityFinding
from .advisories import LoadedTable, evaluate

log = logging.getLogger("vms.ops.security.audit")

DEFAULT_PASSWORDS = frozenset({
    "", "12345", "123456", "1234567", "12345678", "123456789", "1234567890", "admin", "admin123", "admin1234",
    "password", "password1", "888888", "666666", "111111", "000000", "54321", "4321", "1111", "9999", "pass",
    "root", "user", "guest", "default", "hikvision", "dahua", "hik12345", "abcd1234", "qwerty", "admin12345",
    "system", "camera", "ipcam", "vizxv", "jvbzd", "7ujmko0admin", "tlJwpbo6", "meinsm",
})
ADMIN_USERS = frozenset({"admin", "root", "administrator", "supervisor", "888888", "666666"})
SDK_PORTS = {"hikvision": 8000, "dahua": 37777}

TcpCheck = Callable[[str, int, float], Awaitable[bool]]
RtspProbe = Callable[..., Awaitable[Any]]
OnvifProbe = Callable[[str, int, bool, float], Awaitable[bool | None]]
SsdpScan = Callable[[float], Awaitable[set[str]]]


def password_weakness(password: str, username: str) -> str | None:
    """Motivo (en español) si la contraseña guardada es débil o de fábrica. Análisis local, sin red."""
    if password.lower() in DEFAULT_PASSWORDS:
        return "es una contraseña de fábrica o muy común"
    if username and password.lower() == username.lower():
        return "es igual que el nombre de usuario"
    if len(password) < 8:
        return "tiene menos de 8 caracteres"
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 2:
        return "solo usa un tipo de carácter (mezcla letras, números y símbolos)"
    if re.fullmatch(r"(.)\1+", password) or password in "0123456789abcdefghijklmnopqrstuvwxyz":
        return "es una secuencia o un carácter repetido"
    return None


async def _tcp(host: str, port: int, timeout: float) -> bool:
    ok: bool = await dynamic("vms.vendors.rtsp_probe", "tcp_reachable")(host, port, timeout)
    return ok


async def _rtsp(host: str, port: int, path: str, username: str = "", password: str = "", *,
                timeout: float = 4.0) -> Any:
    return await dynamic("vms.vendors.rtsp_probe", "probe_rtsp")(host, port, path, username, password, timeout=timeout)


_GET_PROFILES = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                 '<s:Body><GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/></s:Body></s:Envelope>')


async def onvif_anonymous(host: str, port: int, https: bool, timeout: float) -> bool | None:
    """¿Responde `GetProfiles` SIN credenciales? None = no hay ONVIF o no se puede saber."""
    url = f"{'https' if https else 'http'}://{format_host(host)}:{port}/onvif/Media"
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=False) as c:  # noqa: S501
            r = await c.post(url, content=_GET_PROFILES.encode(),
                             headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    except httpx.HTTPError:
        return None
    if r.status_code == 200 and "Profiles" in r.text:
        return True
    if r.status_code in (400, 401, 403, 500) or "NotAuthorized" in r.text:
        return False
    return None


async def ssdp_scan(timeout: float = 2.0) -> set[str]:
    """IPs que contestan a una búsqueda UPnP (M-SEARCH) desde este PC. Pasiva: no toca ningún equipo."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\n"
           "ST: ssdp:all\r\n\r\n").encode()

    def run() -> set[str]:
        found: set[str] = set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.settimeout(0.3)
            try:
                s.sendto(msg, ("239.255.255.250", 1900))
            except OSError:
                return found
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    _, addr = s.recvfrom(4096)
                    found.add(addr[0])
                except TimeoutError:
                    continue
                except OSError:
                    break
        return found

    return await asyncio.to_thread(run)


@dataclass
class AuditDeps:
    client_factory: Callable[[Device, str], DeviceClient]
    get_password: Callable[[str], str]
    tcp_check: TcpCheck = _tcp
    rtsp_probe: RtspProbe = _rtsp
    onvif_probe: OnvifProbe = onvif_anonymous
    ssdp: SsdpScan | None = ssdp_scan
    timeout: float = 1.5


def _f(device_id: str, check: str, severity: str, status: str, detail: str, action: str = "",
       advisories: list[str] | None = None) -> SecurityFinding:
    return SecurityFinding(device_id=device_id, check=check, severity=severity, status=status,
                           detail_es=detail, action_es=action, advisory_ids=advisories or [])


async def audit_device(dev: Device, cams: list[Camera], deps: AuditDeps, table: LoadedTable,
                       ssdp_hosts: set[str] | None, clock: ClockCheck | None,
                       admin: tuple[str, str] | None) -> list[SecurityFinding]:
    out: list[SecurityFinding] = []
    did = dev.id
    password = deps.get_password(did)
    # 1) contraseña guardada (local)
    if password:
        weak = password_weakness(password, dev.username)
        if weak:
            out.append(_f(did, "weak_password", "critical", "vulnerable", f"La contraseña guardada {weak}.",
                          "Cambia la contraseña en el equipo por una larga y única, y actualízala en el VMS."))
        else:
            out.append(_f(did, "weak_password", "info", "ok", "La contraseña guardada no es de fábrica ni trivial."))
    else:
        out.append(_f(did, "weak_password", "warning", "unknown", "El VMS no tiene contraseña guardada para este "
                                                                  "equipo."))
    if dev.username.lower() in ADMIN_USERS:
        out.append(_f(did, "admin_user", "warning", "vulnerable",
                      f"El VMS entra con el usuario «{dev.username}», que es de administrador.",
                      "Crea en el equipo un usuario de solo lectura (vista en directo y reproducción) y úsalo en el VMS."))
    else:
        out.append(_f(did, "admin_user", "info", "ok", "El VMS usa un usuario propio, no el administrador."))
    # 2) servicios visibles desde la red
    host = dev.host
    ports = {"telnet": 23, "ssh": 22, "http": 80, "https": 443}
    sdk = SDK_PORTS.get(dev.vendor)
    probes = {name: deps.tcp_check(host, p, deps.timeout) for name, p in ports.items()}
    if sdk:
        probes["sdk"] = deps.tcp_check(host, sdk, deps.timeout)
    res = dict(zip(probes, await asyncio.gather(*probes.values()), strict=True))
    sec: DeviceSecuritySettings | None = None
    if admin is not None and SECURITY_READ in capabilities(dev.vendor) and has_api(dev.vendor):
        client: Any = None
        try:
            client = deps.client_factory(dev, password)
            if isinstance(client, DeviceSecurityClient):
                sec = await client.security_settings(admin[0], admin[1])
        except DeviceError as exc:
            out.append(_f(did, "telnet", "info", "unknown",
                          f"No se pudieron leer los ajustes con el usuario administrador: {redact(exc.message)}"))
        except Exception:  # noqa: BLE001
            log.exception("Error leyendo los ajustes de seguridad de %s", did)
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    log.debug("Error cerrando el cliente", exc_info=True)

    telnet_on = sec.telnet_enabled if sec and sec.telnet_enabled is not None else res["telnet"]
    out.append(_f(did, "telnet", "critical", "vulnerable", "Telnet está activado: es un acceso sin cifrar.",
                  "Desactiva Telnet en el equipo (Red > Avanzado).") if telnet_on else
               _f(did, "telnet", "info", "ok", "Telnet no está accesible."))
    ssh_on = sec.ssh_enabled if sec and sec.ssh_enabled is not None else res["ssh"]
    out.append(_f(did, "ssh", "warning", "vulnerable", "SSH está activado en el equipo.",
                  "Desactívalo si no lo usa el servicio técnico.") if ssh_on else
               _f(did, "ssh", "info", "ok", "SSH no está accesible."))
    if res["http"] and not res["https"]:
        out.append(_f(did, "http_no_tls", "warning", "vulnerable",
                      "La web del equipo solo funciona sin cifrar (HTTP): la contraseña viaja en claro por la red.",
                      "Activa HTTPS en el equipo y marca «Usar HTTPS» en el alta."))
    elif res["https"]:
        out.append(_f(did, "http_no_tls", "info", "ok", "La web del equipo admite HTTPS."))
    else:
        out.append(_f(did, "http_no_tls", "info", "unknown", "No responde ningún puerto web estándar (80/443)."))
    if sdk:
        sdk_on = sec.sdk_port_open if sec and sec.sdk_port_open is not None else res["sdk"]
        out.append(_f(did, "sdk_port", "info", "vulnerable" if sdk_on else "ok",
                      f"El puerto del SDK del fabricante ({sdk}) está abierto." if sdk_on else
                      f"El puerto del SDK ({sdk}) no está accesible.",
                      "Ciérralo si ningún programa del fabricante lo usa." if sdk_on else ""))
    upnp_on: bool | None = sec.upnp_enabled if sec and sec.upnp_enabled is not None else (
        None if ssdp_hosts is None else host in ssdp_hosts)
    if upnp_on is None:
        out.append(_f(did, "upnp", "info", "unknown", "No se pudo comprobar UPnP."))
    else:
        out.append(_f(did, "upnp", "warning", "vulnerable", "UPnP está activo: el equipo puede abrir puertos en el "
                                                           "router por su cuenta.", "Desactiva UPnP en el equipo.")
                   if upnp_on else _f(did, "upnp", "info", "ok", "El equipo no responde a búsquedas UPnP."))
    if sec is not None and sec.p2p_cloud_enabled is not None:
        out.append(_f(did, "p2p_cloud", "warning", "vulnerable", "El servicio en la nube del fabricante (P2P) está "
                                                                "activado.", "Desactívalo (Hik-Connect/EZVIZ o P2P de "
                                                                             "Dahua) si no se usa.")
                   if sec.p2p_cloud_enabled else _f(did, "p2p_cloud", "info", "ok", "La nube del fabricante (P2P) "
                                                                                     "está desactivada."))
    else:
        no_read = admin is not None and not (SECURITY_READ in capabilities(dev.vendor) and has_api(dev.vendor))
        out.append(_f(did, "p2p_cloud", "info", "unknown",
                      f"Los equipos «{driver_name(dev.vendor)}» no permiten leer sus ajustes de seguridad desde el "
                      "programa: comprueba en el equipo si la nube del fabricante (P2P) está activa." if no_read else
                      "Para saber si la nube del fabricante (P2P) está activa hacen falta las credenciales de "
                      "administrador."))
    # 3) acceso anónimo: una petición sin credenciales a cada uno
    path = None
    if cams:
        try:
            path = camera_paths(dev, cams[0])[0]
        except ValueError:
            path = None
    if path is None:
        pp = preset_paths(dev.vendor, 1) if dev.vendor in ("hikvision", "dahua") else None
        path = pp[0] if pp else None
    if path:
        r = await deps.rtsp_probe(host, dev.rtsp_port, path, timeout=deps.timeout + 2.5)
        if r.status == 200:
            out.append(_f(did, "anonymous_rtsp", "critical", "vulnerable",
                          "Cualquiera en la red puede ver el vídeo RTSP sin usuario ni contraseña.",
                          "Activa la autenticación RTSP en el equipo (Seguridad > Autenticación RTSP: digest)."))
        elif r.status in (401, 403):
            out.append(_f(did, "anonymous_rtsp", "info", "ok", "El vídeo RTSP pide usuario y contraseña."))
        else:
            out.append(_f(did, "anonymous_rtsp", "info", "unknown", "No se pudo comprobar el acceso anónimo al vídeo."))
    anon_onvif = sec.anonymous_onvif if sec and sec.anonymous_onvif is not None else await deps.onvif_probe(
        host, dev.onvif_port or dev.http_port, dev.https, deps.timeout + 2.5)
    if anon_onvif is True:
        out.append(_f(did, "anonymous_onvif", "critical", "vulnerable", "ONVIF responde sin usuario ni contraseña.",
                      "Activa la autenticación de ONVIF en el equipo o desactiva ONVIF si no se usa."))
    elif anon_onvif is False:
        out.append(_f(did, "anonymous_onvif", "info", "ok", "ONVIF pide usuario y contraseña."))
    else:
        out.append(_f(did, "anonymous_onvif", "info", "unknown", "ONVIF no responde o está desactivado."))
    # 4) firmware frente a la tabla de avisos: nunca «ok» sin saber la marca real, el modelo y el firmware
    out.extend(firmware_findings(dev, table))
    # 5) hora
    if clock is None or clock.status == "unknown":
        out.append(_f(did, "clock", "info", "unknown", clock.message_es if clock else "No hay medida de la hora."))
    else:
        sev = {"ok": "info", "warning": "warning", "critical": "critical"}[clock.status]
        out.append(_f(did, "clock", sev, "ok" if clock.status == "ok" else "vulnerable", clock.message_es))
    return out


def firmware_findings(dev: Device, table: LoadedTable) -> list[SecurityFinding]:
    """Firmware frente a la tabla de avisos (CONTRATO §18.12).

    La marca es la del driver; con «ONVIF (otras marcas)» o RTSP manual, la que dice el propio equipo
    (`Device.manufacturer`) o su modelo (`drivers.brand_from`). Sin marca segura, sin aviso para esa marca en la
    tabla, sin modelo o sin firmware (versión o fecha de build): «Desconocido», nunca «Sin CVE conocidos»."""
    did = dev.id
    out: list[SecurityFinding] = []
    fw_date = dev.firmware_date
    brand = brand_from(dev.vendor, dev.manufacturer, dev.model)
    action_probe = "Pulsa «Probar conexión» en el equipo para leer el modelo y el firmware."
    if brand is None:
        who = f"«{dev.manufacturer}»" if dev.manufacturer else "sin identificar"
        out.append(_f(did, "firmware_cve", "info", "unknown",
                      f"Desconocido: el equipo está dado de alta como «{driver_name(dev.vendor)}» y su marca real "
                      f"({who}) no se puede saber con seguridad, así que no se compara con la tabla de avisos.",
                      "Si es de una marca con driver propio (Hikvision, Dahua…), dalo de alta con esa marca; si no, "
                      "comprueba el firmware en la web del fabricante." if dev.model else action_probe))
        return out
    if not any(a.vendor == brand for a in table.table.advisories):
        out.append(_f(did, "firmware_cve", "info", "unknown",
                      f"Desconocido: la tabla de avisos de esta versión no cubre los equipos «{driver_name(brand)}».",
                      "Comprueba los avisos de seguridad en la web del fabricante y actualiza el firmware."))
        return out
    if not dev.model or not (dev.firmware or fw_date):
        out.append(_f(did, "firmware_cve", "info", "unknown", "No se conoce el modelo o el firmware del equipo.",
                      action_probe))
        return out
    matches = evaluate(table.table, brand, dev.model, dev.firmware, fw_date)
    if not matches:
        out.append(_f(did, "firmware_cve", "info", "ok", "Sin CVE conocidos en la tabla para este modelo y firmware."))
    else:
        for m in matches:
            adv = m.advisory
            kev = " Está en el catálogo KEV: se está explotando activamente." if adv.kev else ""
            if m.verdict == "vulnerable":
                out.append(_f(did, "firmware_cve", "critical", "vulnerable", f"Vulnerable{' (KEV)' if adv.kev else ''}: "
                              f"{m.detail_es}{kev}", "Actualiza el firmware a la última versión del fabricante.",
                              [adv.id]))
            elif m.verdict == "probably_vulnerable":
                out.append(_f(did, "firmware_cve", "critical" if adv.kev else "warning", "probably_vulnerable",
                              f"Probablemente vulnerable (el modelo coincide; confirma la familia): {m.detail_es}{kev}",
                              "Comprueba el modelo en el aviso del fabricante y actualiza el firmware.", [adv.id]))
            elif m.verdict == "unknown":
                out.append(_f(did, "firmware_cve", "warning", "unknown", f"Desconocido: {m.detail_es}",
                              "Comprueba el firmware con el aviso del fabricante.", [adv.id]))
            else:
                out.append(_f(did, "firmware_cve", "info", "ok", f"Sin CVE conocidos en la tabla: {m.detail_es}",
                              "", [adv.id]))
    return out


async def run_audit(site_id: str, devices: list[Device], cameras: list[Camera], deps: AuditDeps,
                    table: LoadedTable, clocks: dict[str | None, ClockCheck],
                    admin_credentials: dict[str, tuple[str, str]]) -> SecurityAuditReport:
    ssdp_hosts: set[str] | None = None
    if deps.ssdp is not None:
        try:
            ssdp_hosts = await deps.ssdp(2.0)
        except OSError as exc:
            log.info("Búsqueda UPnP no disponible: %s", exc)
    sem = asyncio.Semaphore(16)

    async def one(dev: Device) -> list[SecurityFinding]:
        async with sem:
            try:
                return await audit_device(dev, [c for c in cameras if c.device_id == dev.id], deps, table,
                                          ssdp_hosts, clocks.get(dev.id), admin_credentials.get(dev.id))
            except Exception:  # noqa: BLE001 - un equipo raro no tumba la auditoría
                log.exception("Error auditando el equipo %s", dev.id)
                return [_f(dev.id, "firmware_cve", "info", "unknown", "No se pudo auditar este equipo.")]

    findings = [f for chunk in await asyncio.gather(*(one(d) for d in devices if d.enabled)) for f in chunk]
    return SecurityAuditReport(site_id=site_id, advisories_version=table.table.generated_at.date().isoformat()
                               + f" ({table.origin})", findings=findings,
                               devices_checked=sum(1 for d in devices if d.enabled),
                               admin_credentials_used=bool(admin_credentials))
