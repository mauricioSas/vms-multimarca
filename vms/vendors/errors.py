"""Errores de los drivers que amplían los de `vms.core.errors` sin cambiar su contrato.

`DeviceLocked` hereda de `DeviceAuthFailed`: todo el código que ya trata un fallo de credenciales como
«no reintentar» lo sigue haciendo, y quien quiera puede distinguir el bloqueo (PLAN-V2 §3.2 punto 2).
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Any

from vms.core.errors import DeviceAuthFailed, DeviceError, DeviceUnsupported


class DevicePermissionDenied(DeviceUnsupported):
    """ISAPI 403 `lowPrivilege`: la contraseña vale, pero ese usuario no tiene permiso para ese recurso.

    Hereda de `DeviceUnsupported` (mismo código `device_unsupported`) a propósito: quien LEE (salud, hora,
    diagnóstico) lo trata igual que antes, como «este equipo no lo permite». Nunca es `DeviceAuthFailed`: un usuario
    sin permiso para una instantánea o para la hora no es una contraseña mala, así que ni funde el cliente ni pausa
    30 min la API del equipo. «Poner en hora» lo convierte en `DeviceWriteDenied`."""

    def __init__(self, label: str, path: str) -> None:
        super().__init__(f"El usuario de {label} no tiene permiso para {path}", details={"status": 403,
                                                                                      "low_privilege": True})


class DeviceWriteDenied(DeviceError):
    """El usuario entra (la contraseña vale) pero no tiene permiso para CAMBIAR ajustes: ISAPI 403 `lowPrivilege`
    (o un 401 en la escritura justo después de que la misma contraseña valiera para leer), ONVIF `NotAuthorized` en
    una escritura tras una lectura autenticada correcta.

    No hereda de `DeviceAuthFailed` a propósito: no es una contraseña mala, así que ni funde el cliente ni cuenta
    para la espera de 30 min de B6 (el usuario de solo lectura del VMS es lo recomendado, ALTA-EQUIPOS.md).
    `details["auth_rejection"]`: el equipo contestó como a una contraseña mala (401, `NotAuthorized`) y pudo contarlo
    como intento fallido; `details["remaining_attempts"]`, los intentos que le quedan si los dijo."""

    code, status = "device_write_denied", 403

    def __init__(self, label: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"El usuario de {label} puede leer pero no tiene permiso para cambiar ajustes",
                         details=details)


class DeviceTimeUnverified(DeviceError):
    """«Poner en hora»: el equipo ACEPTÓ la hora, pero la lectura de comprobación de después falló (p. ej. dejó de
    responder justo entonces). La hora sí se escribió: quien llama no debe decir que no se pudo poner en hora.
    `details["cause"]` = código del fallo de la lectura y `details["reason"]` = su texto; `details["previous_zone"]`,
    si se sabe, la zona que tenía el equipo antes de escribir (para audit.log: con la zona de este PC, pudo cambiar)."""

    code, status = "clock_set_unverified", 502

    def __init__(self, label: str, cause: DeviceError, *, previous_zone: str | None = None) -> None:
        details: dict[str, Any] = {"cause": cause.code, "reason": cause.message}
        if previous_zone is not None:
            details["previous_zone"] = previous_zone[:64]
        super().__init__(f"{label} aceptó la hora, pero no se pudo leer de nuevo para comprobarla: {cause.message}",
                         details=details)


class DeviceZoneUnsupported(DeviceError):
    """«Usar también la zona horaria de este PC»: el equipo no puede guardar esa zona (su formato no admite esas
    reglas, p. ej. un cambio de hora a las 24:00 en Hikvision) o la rechaza. No se ha escrito nada: ni la zona ni la
    hora. `details["zone"]` = la zona pedida; `details["reason"]` = el motivo, si lo hay."""

    code, status = "clock_zone_unsupported", 422

    def __init__(self, label: str, zone: str, reason: str = "") -> None:
        super().__init__(f"{label} no admite la zona horaria {zone[:64]}" + (f" ({reason[:120]})" if reason else ""),
                         details={"zone": zone[:64], "reason": reason[:120]})


class DeviceZoneNotApplied(DeviceError):
    """«Usar también la zona horaria de este PC»: el equipo aceptó el cambio, pero al leerlo de nuevo sigue con otra
    zona. La hora sí se escribió; `reading` es la lectura de comprobación (con `previous_zone`) y `details` dice la
    zona pedida (`wanted`) y la que tiene (`got`)."""

    code, status = "clock_zone_not_applied", 502

    def __init__(self, label: str, wanted: str, reading: Any) -> None:
        self.reading = reading
        got = str(getattr(reading, "zone", "") or "")[:64]
        super().__init__(f"{label} aceptó la hora, pero sigue con la zona horaria «{got or 'sin informar'}» y no con "
                         f"«{wanted[:64]}»", details={"wanted": wanted[:64], "got": got})


class DeviceClockManaged(DeviceError):
    """La hora del equipo la pone otro sistema (NTP, HikCentral/iVMS por SDK, una plataforma, el grabador, GPS):
    «Poner en hora con este PC» no escribe nada para no pelearse con él. `details["time_mode"]` = modo crudo del
    equipo («NTP», «SDK», «platform», «timecorrect»…) y `details["ntp_server"]` si se sabe."""

    code, status = "device_clock_managed", 409

    def __init__(self, label: str, time_mode: str, ntp_server: str = "") -> None:
        self.time_mode, self.ntp_server = time_mode, ntp_server
        super().__init__(f"La hora de {label} la pone otro sistema (modo «{time_mode}»)",
                         details={"time_mode": time_mode, "ntp_server": ntp_server})


class DeviceLocked(DeviceAuthFailed):
    """El equipo dice que el usuario está bloqueado por demasiados intentos fallidos."""

    code = "device_locked"

    def __init__(self, label: str, minutes: int | None, *, details: dict[str, Any] | None = None) -> None:
        self.minutes = minutes
        wait = (f"Espera {minutes} {'minuto' if minutes == 1 else 'minutos'} antes de volver a probar"
                if minutes else "Espera a que el equipo lo desbloquee (suele ser 30 minutos)")
        super().__init__(f"El usuario está bloqueado en {label} por demasiados intentos fallidos. {wait}; "
                         "no reintentes antes o el bloqueo se alarga.",
                         details={"locked": True, "lockout_minutes": minutes, **(details or {})})


class BasicNotAllowed(DeviceAuthFailed):
    """El equipo solo ofrece autenticación Basic y este equipo no la tiene permitida."""

    code = "device_basic_not_allowed"

    def __init__(self, label: str) -> None:
        super().__init__(f"{label} solo admite autenticación Basic, que envía la contraseña sin cifrar. "
                         "Activa Digest en el equipo o marca «Permitir autenticación Basic» en este equipo "
                         "si aceptas el riesgo.", details={"basic_only": True})


def bad_password_message(label: str, remaining: int | None = None) -> str:
    extra = (f" Te quedan {remaining} {'intento' if remaining == 1 else 'intentos'} antes de que el equipo "
             "bloquee el usuario." if remaining is not None else
             " Ojo: tras varios intentos fallidos el equipo puede bloquear el usuario un rato.")
    return f"Usuario o contraseña incorrectos en {label}.{extra}"


# --------------------------------------------------------------------------- Hikvision (ISAPI)
def hik_user_check(body: str | bytes) -> tuple[bool, int | None, int | None] | None:
    """Lee el `<userCheck>` que devuelve ISAPI con un 401: (bloqueado, minutos, intentos restantes).

    Formato (firmware 5.x; **no verificado con hardware**, se fija con fixtures reales):
    `<userCheck><statusValue>401</statusValue><lockStatus>lock|unlock</lockStatus>
    <unlockTime>segundos</unlockTime><retryLoginTime>intentos</retryLoginTime></userCheck>`.
    None si el cuerpo no es un userCheck."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    if "userCheck" not in text:
        return None
    try:
        root = ET.fromstring(text.strip().encode("utf-8"))
    except ET.ParseError:
        return None
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    if root.tag != "userCheck":
        return None

    def num(tag: str) -> int | None:
        value = (root.findtext(tag) or "").strip()
        return int(value) if value.isdigit() else None

    locked = (root.findtext("lockStatus") or "").strip().lower() == "lock"
    unlock_s = num("unlockTime")
    minutes = max(1, math.ceil(unlock_s / 60)) if locked and unlock_s else None
    return locked, minutes, num("retryLoginTime")


# --------------------------------------------------------------------------- Dahua (CGI/RPC2) y RTSP
_LOCK_RE = re.compile(r"\block(?:ed)?\b|user\s*locked|account\s*locked|blocked", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d{1,4})\s*(?:min|minutes?|minutos?)", re.IGNORECASE)
_SECONDS_RE = re.compile(r"(?:remainLockSecond|unlockTime|lock(?:ed)?\s*for)\D{0,5}(\d{1,6})", re.IGNORECASE)


def text_lock_hint(text: str) -> tuple[bool, int | None]:
    """¿El texto de un 401/403 dice «bloqueado»? Dahua CGI responde «Error … Locked» (**no verificado con
    hardware**; las respuestas exactas se fijan con fixtures reales). Devuelve (bloqueado, minutos)."""
    if not text or not _LOCK_RE.search(text) or "unlock" in text.lower() and "locked" not in text.lower():
        return False, None
    m = _MINUTES_RE.search(text)
    if m:
        return True, int(m.group(1))
    s = _SECONDS_RE.search(text)
    if s:
        return True, max(1, math.ceil(int(s.group(1)) / 60))
    return True, None


__all__ = ["BasicNotAllowed", "DeviceClockManaged", "DeviceLocked", "DevicePermissionDenied", "DeviceTimeUnverified",
           "DeviceWriteDenied", "DeviceZoneNotApplied", "DeviceZoneUnsupported", "bad_password_message",
           "hik_user_check", "text_lock_hint"]
