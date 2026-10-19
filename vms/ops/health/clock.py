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
  Si no se puede medir, se informa del modo (`ntp`/`manual`) sin inventar un número. Un servidor que dice que
  él mismo no está en hora (LI = 3 o estrato 0, RFC 4330) no vale como referencia.

Umbrales: `settings.health.clock_warn_s` (2 s) y `clock_critical_s` (30 s). También se avisa si el
equipo está en hora manual (sin NTP): con el tiempo deriva.

El desfase se mide SIEMPRE en UTC, igual en todas las marcas: un equipo con NTP bien y la zona de fábrica
(p. ej. China, UTC+8) tiene el reloj en hora. Pero la hora que sobreimprime en la imagen va en su zona: si el
driver la conoce (`DeviceTime.utc_offset_s`) y no coincide con la del PC, es un aviso aparte (`warning`). Con la
hora puesta a mano y una marca con `time_write`, se arregla con la casilla «Usar también la zona horaria de este PC»
de «Poner en hora con este PC»; si no, en la web del equipo. `ClockCheck.utc_offset_s` guarda la zona del equipo en
el momento de la medida para el diálogo de ese botón.

Textos honestos (revisión de la hora de las cámaras):
- Las grabaciones llevan la hora de ESTE PC (MediaMTX sella cada fotograma al recibirlo:
  `useAbsoluteTimestamp: false`, fijado en `vms/engine/mtx_config.py`). Un equipo desajustado solo estropea la
  hora que escribe en la imagen y lo que guarde él mismo; se dice así, sin asustar con «grabaciones sin valor».
- Una fecha anterior a `NEVER_SET_YEAR` (01-01-1970, 01-01-2000…) es un reloj que nunca se puso en hora (suele
  pasar al quedarse sin corriente): se dice eso y no «va 497 627 h atrasada».
- «Activa NTP apuntando a este PC» solo si este PC de verdad sirve la hora (W32Time con `NtpServer` activado);
  si no, el consejo era imposible de seguir. Lo que sí se puede hacer siempre: «Poner en hora con este PC».
- Cada aviso lleva qué hacer. `Assessment` separa la causa de la acción: los textos de salud las unen con
  «Qué hacer:» y «¿Por qué no conecta?» usa la acción sola (la interfaz ya pone el prefijo).
"""
from __future__ import annotations

import logging
import socket
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from vms.core.interfaces import DeviceTime
from vms.core.tzposix import PosixZone, posix_zone

from ..models import ClockCheck

log = logging.getLogger("vms.ops.clock")

NTP_EPOCH_OFFSET = 2208988800   # segundos entre 1900-01-01 y 1970-01-01
NEVER_SET_YEAR = 2015           # un reloj por debajo de este año no se ha puesto nunca en hora
Status = Literal["ok", "warning", "critical", "unknown"]

SET_BUTTON = "«Poner en hora con este PC»"
ZONE_BOX = "«Usar también la zona horaria de este PC»"     # la casilla del diálogo de SET_BUTTON
# el botón es solo para administradores y sale en la fila cuando hay una medida con aviso (de ahí «Comprobar ahora»)
SET_ACTION = (f"En Estado del sistema > Hora de los equipos, pulsa {SET_BUTTON} (hace falta ser administrador del "
              "programa; si el botón no aparece, pulsa antes «Comprobar ahora»).")
PC_SYNC_ACTION = ("En Windows, abre Configuración > Hora e idioma > Fecha y hora, activa «Establecer la hora "
                  "automáticamente» y pulsa «Sincronizar ahora».")
PC_SERVER_ACTION = ("En el Panel de control de Windows, abre Fecha y hora > Hora de Internet > Cambiar la "
                    "configuración y elige otro servidor de hora.")


def what_to_do(action: str) -> str:
    """« Qué hacer: …» con la acción en minúscula tras los dos puntos (las acciones van con mayúscula porque
    «¿Por qué no conecta?» las pinta solas)."""
    return f" Qué hacer: {action[:1].lower()}{action[1:]}" if action else ""


@dataclass(frozen=True)
class Assessment:
    """Veredicto de la hora de un equipo: estado, causa y acción por separado (sin el prefijo «Qué hacer:»)."""

    status: Status
    cause: str
    action: str = ""

    @property
    def message(self) -> str:
        return self.cause + what_to_do(self.action)


def _amount(a: float) -> str:
    if a < 60:
        return f"{a:.1f} s".replace(".", ",")
    if a < 3600:
        return f"{a / 60:.0f} min"
    if a < 86400:
        return f"{a / 3600:.1f} h".replace(".", ",")
    if a < 365 * 86400:
        n = round(a / 86400)
        return f"{n} {'día' if n == 1 else 'días'}"
    n = round(a / (365.25 * 86400))
    return f"{n} {'año' if n == 1 else 'años'}"


def _fmt_skew(skew: float) -> str:
    """«12,0 s adelantada», «2,0 h atrasada», «3 días adelantada», «56 años atrasada»."""
    return f"{_amount(abs(skew))} {'adelantada' if skew > 0 else 'atrasada'}"


def fmt_skew(skew: float | None) -> str:
    """Desfase para tablas e informes («+12,0 s», «−56 años»); «—» si no se sabe."""
    if skew is None:
        return "—"
    return f"{'+' if skew >= 0 else '−'}{_amount(abs(skew))}"


def cap_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def de_noun(noun: str) -> str:
    """«de» + artículo con la contracción del español: «del equipo», «de la cámara»."""
    return f"del {noun[3:]}" if noun.startswith("el ") else f"de {noun}"


def pon_en_hora(noun: str) -> str:
    """«ponla en hora» (la cámara) o «ponlo en hora» (el grabador, el equipo)."""
    return f"pon{'la' if noun.startswith('la ') else 'lo'} en hora"


def _date_es(instant: datetime) -> str:
    """Fecha tal como la tiene el equipo («01-01-1970»), sin pasar por la zona del PC (falla antes de 1970)."""
    return f"{instant.day:02d}-{instant.month:02d}-{instant.year}"


def assess(skew_s: float | None, time_mode: str, warn_s: float, critical_s: float, *,
           device_time: datetime | None = None, zone: str | None = None, can_set: bool = True,
           pc_serves_time: bool = False, ntp_server: str = "", noun: str = "el equipo") -> Assessment:
    """Estado, causa y acción para la hora de un equipo.

    `device_time`: hora del equipo (para reconocer un reloj nunca puesto); `zone`: frase de `zone_mismatch` si su
    zona no es la del PC; `can_set`: el driver tiene «Poner en hora con este PC» (`time_write`); `pc_serves_time`:
    este PC sirve la hora por NTP (solo entonces se aconseja NTP apuntando a él); `noun`: «la cámara», «el grabador»…"""
    if skew_s is None:
        return Assessment("unknown", "No se pudo leer la hora del equipo.")
    a = abs(skew_s)
    never_set = device_time is not None and device_time.year < NEVER_SET_YEAR
    status: Status = "critical" if never_set or a >= critical_s else "warning" if a >= warn_s else "ok"
    recordings_ok = (f" Las grabaciones usan la hora de este PC y son correctas; lo que sale mal es la hora que "
                     f"{noun} escribe en la imagen y lo que guarde en su memoria.")
    if never_set:
        assert device_time is not None
        cause = (f"{cap_first(noun)} no tiene la hora puesta: marca el {_date_es(device_time)} (suele pasar cuando se "
                 "queda sin corriente)." + recordings_ok)
    elif status == "critical":
        cause = f"La hora {de_noun(noun)} va {_fmt_skew(skew_s)} respecto a este PC." + recordings_ok
    elif status == "warning":
        cause = f"La hora {de_noun(noun)} va {_fmt_skew(skew_s)} respecto a este PC."
    else:
        cause = "La hora coincide con la de este PC."

    web = f"{pon_en_hora(noun)} desde la web {de_noun(noun)} (apartado de fecha y hora)"
    ntp_hint = (" Para que no se vuelva a desajustar, también puedes activar NTP en el equipo con la IP de este PC: "
                "este PC sirve la hora.") if pc_serves_time else ""
    action = ""
    press_now = status != "ok" and time_mode == "manual" and can_set    # la acción ya es pulsar el botón
    if status != "ok":
        if time_mode == "ntp":
            server = f" ({ntp_server})" if ntp_server else ""
            cause += f" {cap_first(noun)} toma la hora de un servidor de hora NTP{server} que no se la da bien."
            action = (f"Revisa que {noun} llegue a ese servidor de hora o pon otro en su web (apartado de fecha y "
                      "hora). El programa no cambia la hora de un equipo que la toma de un servidor.")
        elif time_mode == "unknown":
            action = ("Si otro programa o el grabador pone la hora a este equipo (HikCentral, iVMS-4200…), "
                      f"corrígela allí; si no, {web}.")
        else:
            action = (SET_ACTION if can_set else cap_first(web) + ".") + ntp_hint
    elif time_mode == "manual":
        status = "warning"
        cause += f" {cap_first(noun)} tiene la hora puesta a mano (sin NTP): con los días se desajustará."
        action = ("Revisa su hora de vez en cuando en Estado del sistema > Hora de los equipos y, si se desajusta, " +
                  (f"pulsa {SET_BUTTON}." if can_set else f"{web}.") + ntp_hint)
    if zone is not None:
        cause += f" Además, {zone[:1].lower()}{zone[1:]}"
        if can_set and time_mode == "manual":
            # el botón también cambia la zona (la casilla del diálogo); solo sale con la hora puesta a mano
            if press_now:
                action = action.replace(SET_ACTION, f"{SET_ACTION} En ese diálogo, deja marcada la casilla {ZONE_BOX} "
                                                    "para corregir también la zona.", 1)
            else:
                action = (f"Para corregir la zona, en Estado del sistema > Hora de los equipos pulsa {SET_BUTTON} "
                          f"(hace falta ser administrador del programa) y deja marcada la casilla {ZONE_BOX}. "
                          + action)
        else:
            # con NTP u otro sistema el botón no escribe nada (y algunas marcas no lo tienen): la web del equipo
            zone_action = (f"cambia la zona horaria en la web {de_noun(noun)} (apartado de fecha y hora) a la de la "
                           "tienda.")
            action = f"{action} Después, {zone_action}" if action else cap_first(zone_action)
        status = "warning" if status == "ok" else status
    return Assessment(status, cause, action.strip())


def evaluate(skew_s: float | None, time_mode: str, warn_s: float, critical_s: float) -> tuple[Status, str]:
    """Estado y frase de tienda para un desfase (con «Qué hacer:» si hay que hacer algo)."""
    a = assess(skew_s, time_mode, warn_s, critical_s)
    return a.status, a.message


def evaluate_pc(skew_s: float, time_mode: str, warn_s: float, critical_s: float, server: str) -> tuple[Status, str]:
    """Estado y frase para la hora del PROPIO PC (las acciones son de Windows, no de una cámara)."""
    a = abs(skew_s)
    action = ""
    if a >= critical_s:
        status: Status = "critical"
        msg = (f"La hora del PC va {_fmt_skew(skew_s)} respecto al servidor de hora {server}: las grabaciones y la "
               "hora que se pone en las cámaras salen de este PC.")
        action = ("Activa la sincronización de hora de Windows (Configuración > Hora e idioma > Fecha y hora > "
                  "«Sincronizar ahora»).")
    elif a >= warn_s:
        status = "warning"
        msg = f"La hora del PC va {_fmt_skew(skew_s)} respecto al servidor de hora {server}."
        action = ("Revisa que la sincronización de hora de Windows esté activada (Configuración > Hora e idioma > "
                  "Fecha y hora).")
    else:
        status = "ok"
        msg = f"La hora del PC coincide con el servidor de hora {server}."
    if time_mode == "manual":
        if status == "ok":
            status = "warning"
        msg += " Windows no sincroniza la hora (W32Time sin NTP): con los días se desajustará."
        action = action or PC_SYNC_ACTION
    return status, msg + what_to_do(action)


def _fmt_offset(seconds: int) -> str:
    sign = "+" if seconds >= 0 else "−"
    h, rem = divmod(abs(seconds), 3600)
    return f"UTC{sign}{h}" + (f":{rem // 60:02d}" if rem else "")


fmt_offset = _fmt_offset   # «UTC+2», «UTC−3», «UTC+5:30»


def zone_label_es(rule: PosixZone) -> str:
    """Zona en lenguaje de tienda: «UTC+1 en invierno y UTC+2 en verano» o «UTC+8, sin horario de verano»."""
    std = _fmt_offset(int(rule.std.total_seconds()))
    if rule.dst is None:
        return f"{std}, sin horario de verano"
    return f"{std} en invierno y {_fmt_offset(int(rule.dst.total_seconds()))} en verano"


def _device_zone(dt: DeviceTime) -> tuple[int | None, bool]:
    """(zona del equipo en segundos, ¿sale de la regla de su zona en el instante de la medida?)."""
    rule = posix_zone(dt.zone) if dt.zone else None
    off = rule.offset_at_utc(dt.measured_at) if rule is not None else None
    if off is not None:
        return int(round(off.total_seconds() / 900.0) * 900), True
    return dt.utc_offset_s, False


def device_zone_offset_s(dt: DeviceTime) -> int | None:
    """Zona del equipo AHORA (en el instante de la medida, con su horario de verano), en segundos; None si no se
    sabe. Con la regla de su zona (`DeviceTime.zone`) si se entiende: un equipo con la fecha de 1970 y la zona de
    Madrid usa UTC+2 en verano, aunque su propia hora caiga en invierno. Si no, el desfase de su hora local."""
    return _device_zone(dt)[0]


def _pc_offset_s(instant: datetime) -> int:
    """Zona del PC en ese instante. Con un equipo sin hora (1970…) Windows no sabe pasarlo a hora local
    (`localtime` falla antes de 1970): se usa la zona actual del PC."""
    try:
        off = instant.astimezone().utcoffset()
    except (OSError, OverflowError, ValueError):
        off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds()) if off is not None else 0


def pc_offset_s(instant: datetime) -> int:
    """Zona de este PC en ese instante, en segundos (UTC+2 = 7200)."""
    return _pc_offset_s(instant)


def zone_mismatch(dt: DeviceTime, pc_offset_s: int | None = None, noun: str = "el equipo") -> str | None:
    """Frase si la zona del equipo (la de la hora que sale en la imagen) no es la del PC; None si coincide o
    no se sabe. `pc_offset_s`: zona del PC en ese instante (por defecto, la del sistema).

    Solo si el driver da el desfase de la hora local (`utc_offset_s`; ONVIF no lo da: sin aviso de zona). Si la
    regla de su zona se entiende, se comparan las dos zonas en el instante de la medida, igual que el diálogo de
    «Poner en hora» (`device_zone_offset_s` frente a `pc_zone`). Antes se miraba el desfase de su propia hora: un
    equipo con la zona de Madrid y la fecha de 1970 salía en la fila como «UTC+1, distinta de la de este PC (UTC+2)»
    en verano, y el diálogo decía «ya usa la misma zona». Si no se entiende, su desfase y el del PC a su hora."""
    if dt.utc_offset_s is None:
        return None
    dev_offset, by_rule = _device_zone(dt)
    if dev_offset is None:
        return None
    if pc_offset_s is None:
        pc_offset_s = _pc_offset_s(dt.measured_at if by_rule else dt.device_time)
    if abs(dev_offset - pc_offset_s) < 60:
        return None
    return (f"{cap_first(noun)} usa una zona horaria ({_fmt_offset(dev_offset)}) distinta de la de este PC "
            f"({_fmt_offset(pc_offset_s)}): la hora que sale en la imagen no es la de la tienda.")


def assess_device(dt: DeviceTime, warn_s: float, critical_s: float, pc_offset_s: int | None = None, *,
                  can_set: bool = True, noun: str = "el equipo", pc_serves_time: bool | None = None) -> Assessment:
    """`assess` con una lectura del driver. `pc_serves_time=None`: se mira en el registro de Windows."""
    return assess(dt.skew_s, dt.time_mode, warn_s, critical_s, device_time=dt.device_time,
                  zone=zone_mismatch(dt, pc_offset_s, noun), can_set=can_set, ntp_server=dt.ntp_server, noun=noun,
                  pc_serves_time=pc_time_server_enabled() if pc_serves_time is None else pc_serves_time)


def device_check(device_id: str, camera_id: str | None, dt: DeviceTime, warn_s: float,
                 critical_s: float, pc_offset_s: int | None = None, *, can_set: bool = True,
                 noun: str = "el equipo", pc_serves_time: bool | None = None) -> ClockCheck:
    a = assess_device(dt, warn_s, critical_s, pc_offset_s, can_set=can_set, noun=noun, pc_serves_time=pc_serves_time)
    return ClockCheck(camera_id=camera_id, device_id=device_id, at=dt.measured_at, skew_s=round(dt.skew_s, 3),
                      round_trip_ms=round(dt.round_trip_ms, 1), time_mode=dt.time_mode, status=a.status,
                      message_es=a.message, utc_offset_s=device_zone_offset_s(dt))


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


def pc_time_server_enabled() -> bool:
    """¿Este PC sirve la hora por NTP (W32Time `TimeProviders\\NtpServer\\Enabled` = 1)? En un Windows normal
    viene desactivado (0): entonces aconsejar «NTP apuntando a este PC» es imposible de seguir. Fuera de Windows,
    o si no se puede leer: False (no se aconseja lo que no se ha comprobado)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer") as key:
            return int(winreg.QueryValueEx(key, "Enabled")[0]) == 1
    except (OSError, ValueError, TypeError) as exc:
        log.debug("No se pudo saber si este PC sirve la hora: %s", exc)
        return False


class UnsyncedServer(OSError):
    """El servidor de hora contestó, pero dice que él mismo no está en hora (LI = 3 o estrato 0, RFC 4330)."""


def sntp_offset(server: str, *, port: int = 123, timeout: float = 2.0) -> tuple[float, float]:
    """(desfase del PC en s: + si el PC va adelantado, ida y vuelta en ms). Lanza OSError si no responde y
    `UnsyncedServer` si el servidor no está sincronizado (su hora no vale como referencia)."""
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
    leap, stratum = data[0] >> 6, data[1]
    if leap == 3 or stratum == 0:
        raise UnsyncedServer(f"servidor no sincronizado (LI={leap}, estrato={stratum})")

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
    manual_action = what_to_do(PC_SYNC_ACTION)
    if target:
        try:
            skew, rtt = sntp_offset(target)
            status, msg = evaluate_pc(skew, mode, warn_s, critical_s, target)
            return ClockCheck(at=datetime.now(timezone.utc), skew_s=round(skew, 3), round_trip_ms=round(rtt, 1),
                              time_mode=mode, status=status, message_es=msg)
        except OSError as exc:
            log.info("Sin respuesta válida del servidor de hora %s: %s", target, exc)
            unsynced = isinstance(exc, UnsyncedServer)
            msg = (f"El servidor de hora {target} dice que él mismo no está en hora: no sirve para comprobar la hora "
                   "del PC." if unsynced else
                   f"No responde el servidor de hora {target}: no se pudo comprobar la hora del PC.")
            if mode == "manual":
                return ClockCheck(time_mode=mode, status="warning", message_es=msg + " Windows no sincroniza la "
                                  "hora (W32Time sin NTP): con los días se desajustará." + manual_action)
            action = PC_SERVER_ACTION if unsynced else (
                "Comprueba que este PC tiene conexión a Internet; si la tiene, en el Panel de control de Windows abre "
                "Fecha y hora > Hora de Internet > Cambiar la configuración y elige otro servidor de hora.")
            return ClockCheck(time_mode=mode, status="unknown", message_es=msg + what_to_do(action))
    if mode == "manual":
        return ClockCheck(time_mode=mode, status="warning",
                          message_es="Este PC no sincroniza su hora (W32Time sin NTP): con los días se desajustará, y "
                                     "las grabaciones y la hora que se pone en las cámaras salen de este PC."
                                     + manual_action)
    if not sntp:
        return ClockCheck(time_mode=mode, status="unknown",
                          message_es="La comprobación de la hora del PC con el servidor de hora está desactivada.")
    return ClockCheck(time_mode=mode, status="unknown",
                      message_es="No hay un servidor de hora configurado para comprobar el reloj del PC.")
