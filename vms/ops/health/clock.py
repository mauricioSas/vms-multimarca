"""Hora: desfase cámara↔PC y estado de sincronización del propio PC (CONTRATO §18.3).

- La hora del equipo la lee el driver (capacidad `time_read` de B5, `DeviceClockClient.device_time()`):
  ONVIF `GetSystemDateAndTime` (sin autenticación), ISAPI `/ISAPI/System/time` o Dahua `getCurrentTime`.
  Desfase = hora del equipo − hora del PC a mitad del viaje de ida y vuelta (lo calcula el driver en
  `DeviceTime.measured_at`).
- La hora del PC: si hay un servidor NTP conocido (Windows: `W32Time\\Parameters\\NtpServer` del registro,
  sin analizar texto de `w32tm`), se mide el desfase con una consulta SNTP de 48 bytes (RFC 4330), una vez por
  hora. Es el MISMO servidor que ya consulta Windows por su cuenta (en un PC sin dominio, `time.windows.com`):
  no se abre ningún destino nuevo, pero sí es una consulta UDP 123 más hacia Internet. Se puede desactivar
  (`PUT /api/clock/settings` `{"pc_sntp_enabled": false}`); entonces solo se informa del modo.
  Si no se puede medir, se informa del modo (`ntp`/`manual`) sin inventar un número.

Umbrales: `settings.health.clock_warn_s` (2 s) y `clock_critical_s` (30 s). También se avisa si el
equipo está en hora manual (sin NTP): con el tiempo deriva.
"""
from __future__ import annotations

import logging
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Literal

from vms.core.interfaces import DeviceTime

from ..models import ClockCheck

log = logging.getLogger("vms.ops.clock")

NTP_EPOCH_OFFSET = 2208988800   # segundos entre 1900-01-01 y 1970-01-01
Status = Literal["ok", "warning", "critical", "unknown"]


def _fmt_skew(skew: float) -> str:
    a = abs(skew)
    if a < 60:
        amount = f"{a:.1f} s".replace(".", ",")
    elif a < 3600:
        amount = f"{a / 60:.0f} min"
    else:
        amount = f"{a / 3600:.1f} h".replace(".", ",")
    return f"{amount} {'adelantada' if skew > 0 else 'atrasada'}"


def evaluate(skew_s: float | None, time_mode: str, warn_s: float, critical_s: float) -> tuple[Status, str]:
    """Estado y frase de tienda para un desfase."""
    if skew_s is None:
        return "unknown", "No se pudo leer la hora del equipo."
    a = abs(skew_s)
    if a >= critical_s:
        status: Status = "critical"
        msg = (f"La hora va {_fmt_skew(skew_s)}: las grabaciones tendrán la hora equivocada y pueden no valer "
               "como prueba. Activa NTP en el equipo apuntando a este PC.")
    elif a >= warn_s:
        status = "warning"
        msg = f"La hora va {_fmt_skew(skew_s)}. Revisa que el equipo use NTP."
    else:
        status = "ok"
        msg = "La hora coincide con la del PC."
    if time_mode == "manual":
        if status == "ok":
            status = "warning"
        msg += " El equipo tiene la hora puesta a mano (sin NTP): con los días se desajustará."
    return status, msg


def evaluate_pc(skew_s: float, time_mode: str, warn_s: float, critical_s: float, server: str) -> tuple[Status, str]:
    """Estado y frase para la hora del PROPIO PC (las acciones son de Windows, no de una cámara)."""
    a = abs(skew_s)
    if a >= critical_s:
        status: Status = "critical"
        msg = (f"La hora del PC va {_fmt_skew(skew_s)} respecto al servidor de hora {server}: las cámaras y las "
               "grabaciones pueden quedar con la hora equivocada. Activa la sincronización de hora de Windows "
               "(Configuración > Hora e idioma > Sincronizar ahora).")
    elif a >= warn_s:
        status = "warning"
        msg = (f"La hora del PC va {_fmt_skew(skew_s)} respecto al servidor de hora {server}. Revisa que la "
               "sincronización de hora de Windows esté activada.")
    else:
        status = "ok"
        msg = f"La hora del PC coincide con el servidor de hora {server}."
    if time_mode == "manual":
        if status == "ok":
            status = "warning"
        msg += " Windows no sincroniza la hora (W32Time sin NTP): con los días se desajustará."
    return status, msg


def device_check(device_id: str, camera_id: str | None, dt: DeviceTime, warn_s: float,
                 critical_s: float) -> ClockCheck:
    skew = dt.skew_s
    status, msg = evaluate(skew, dt.time_mode, warn_s, critical_s)
    return ClockCheck(camera_id=camera_id, device_id=device_id, at=dt.measured_at, skew_s=round(skew, 3),
                      round_trip_ms=round(dt.round_trip_ms, 1), time_mode=dt.time_mode, status=status,
                      message_es=msg)


def failed_check(device_id: str, camera_id: str | None, message: str) -> ClockCheck:
    return ClockCheck(camera_id=camera_id, device_id=device_id, status="unknown", message_es=message)


# --------------------------------------------------------------------------- hora del PC
def windows_time_config() -> tuple[Literal["ntp", "manual", "unknown"], str]:
    """(modo, servidor) leídos del registro de W32Time. Fuera de Windows: («unknown», «»)."""
    if sys.platform != "win32":
        return "unknown", ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\W32Time\Parameters") as key:
            kind = str(winreg.QueryValueEx(key, "Type")[0]).upper()
            try:
                server = str(winreg.QueryValueEx(key, "NtpServer")[0])
            except OSError:
                server = ""
    except OSError as exc:
        log.debug("No se pudo leer la configuración de W32Time: %s", exc)
        return "unknown", ""
    mode: Literal["ntp", "manual", "unknown"] = "manual" if kind == "NOSYNC" else "ntp"
    # «time.windows.com,0x9 otro,0x8» → primer servidor sin las banderas
    first = server.split()[0].split(",")[0] if server.strip() else ""
    return mode, first


def sntp_offset(server: str, *, port: int = 123, timeout: float = 2.0) -> tuple[float, float]:
    """(desfase del PC en s: + si el PC va adelantado, ida y vuelta en ms). Lanza OSError si no responde."""
    packet = bytearray(48)
    packet[0] = 0x23   # LI=0, VN=4, modo=3 (cliente)
    t1 = time.time()
    struct.pack_into("!II", packet, 40, int(t1 + NTP_EPOCH_OFFSET), int((t1 % 1) * 2**32))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(bytes(packet), (server, port))
        data, _ = s.recvfrom(512)
    t4 = time.time()
    if len(data) < 48:
        raise OSError("Respuesta NTP demasiado corta")

    def ts(offset: int) -> float:
        sec, frac = struct.unpack_from("!II", data, offset)
        return float(sec - NTP_EPOCH_OFFSET + frac / 2**32)

    t2, t3 = ts(32), ts(40)
    offset = ((t2 - t1) + (t3 - t4)) / 2    # cuánto va adelantado el servidor respecto al PC
    return -offset, (t4 - t1 - (t3 - t2)) * 1000


def pc_check(warn_s: float, critical_s: float, *, server: str | None = None, sntp: bool = True) -> ClockCheck:
    """Estado de la hora del propio PC (para el informe y el manifiesto de evidencias). `sntp=False`: sin
    consulta de red, solo el modo de sincronización de Windows."""
    mode, configured = windows_time_config()
    target = (server or configured) if sntp else ""
    if target:
        try:
            skew, rtt = sntp_offset(target)
            status, msg = evaluate_pc(skew, mode, warn_s, critical_s, target)
            return ClockCheck(at=datetime.now(timezone.utc), skew_s=round(skew, 3), round_trip_ms=round(rtt, 1),
                              time_mode=mode, status=status, message_es=msg)
        except OSError as exc:
            log.info("Sin respuesta del servidor de hora %s: %s", target, exc)
            return ClockCheck(time_mode=mode, status="warning" if mode == "manual" else "unknown",
                              message_es=f"No responde el servidor de hora {target}: no se pudo comprobar la hora "
                                         "del PC.")
    if mode == "manual":
        return ClockCheck(time_mode=mode, status="warning",
                          message_es="El PC no sincroniza la hora (W32Time sin NTP). Las cámaras toman la hora de "
                                     "este PC: actívalo.")
    if not sntp:
        return ClockCheck(time_mode=mode, status="unknown",
                          message_es="La comprobación de la hora del PC con el servidor de hora está desactivada.")
    return ClockCheck(time_mode=mode, status="unknown",
                      message_es="No hay un servidor de hora configurado para comprobar el reloj del PC.")
