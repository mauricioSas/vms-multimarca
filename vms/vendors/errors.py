"""Errores de los drivers que amplían los de `vms.core.errors` sin cambiar su contrato.

`DeviceLocked` hereda de `DeviceAuthFailed`: todo el código que ya trata un fallo de credenciales como
«no reintentar» lo sigue haciendo, y quien quiera puede distinguir el bloqueo (PLAN-V2 §3.2 punto 2).
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Any

from vms.core.errors import DeviceAuthFailed


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


__all__ = ["BasicNotAllowed", "DeviceLocked", "bad_password_message", "hik_user_check", "text_lock_hint"]
