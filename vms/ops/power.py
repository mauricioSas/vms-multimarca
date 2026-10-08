"""Energía del PC: batería, suspensión y tapa (Estado del sistema, CONTRATO §6.8, campo `power`).

Mientras Windows tiene el PC suspendido, hibernado o apagado no se graba ninguna cámara: MediaMTX se queda
congelado y, en los portátiles con espera moderna («S0 low power idle»), Windows además desconecta la red.
Se vio en un portátil de tienda (8-oct-2026): dos huecos de grabación con el PC a batería (se suspendía a
los 3 min sin uso) y una noche enchufado con la tapa cerrada.

Esta comprobación solo LEE, sin permisos de administrador y sin cambiar nada de Windows:
- si el PC funciona con batería (`GetSystemPowerStatus`);
- del plan de energía activo, la suspensión y la hibernación por inactividad con corriente y con batería
  (`STANDBYIDLE`, `HIBERNATEIDLE`) y qué hace al cerrar la tapa (`LIDACTION`), con
  `PowerRead{AC,DC}ValueIndex` de powrprof.dll. Si una directiva de grupo fija el valor
  (`HKLM\\SOFTWARE\\Policies\\Microsoft\\Power\\PowerSettings\\<GUID>`), manda la directiva, como en Windows;
- si el PC tiene tapa, batería y espera moderna (`GetPwrCapabilities`).

Los avisos (con «Qué hacer:») se arman con los valores leídos, nunca con plazos fijos. «Apagar la pantalla»
(`VIDEOIDLE`) no se mira: con la pantalla apagada se sigue grabando. Fuera de Windows no hay comprobación
(`read_snapshot()` → None) y `GET /api/status` no trae `power`.
Sin dependencias: `ctypes` y `winreg` de la biblioteca estándar.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("vms.ops.power")

# GUID de los subgrupos y ajustes del plan de energía (los mismos alias que `powercfg /q`)
SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
STANDBYIDLE = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"
HIBERNATEIDLE = "9d7815a6-7ee4-497e-8888-515a05f02364"
SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"
LIDACTION = "5ca83367-6e45-459f-a27b-476b1d01c936"
POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Power\PowerSettings"

# LIDACTION: 0 no hacer nada, 1 suspender, 2 hibernar, 3 apagar
LID_ACTIONS = {0: "nothing", 1: "sleep", 2: "hibernate", 3: "shutdown"}
_LID_DOES = {1: "suspende el PC", 2: "hiberna el PC", 3: "apaga el PC"}

SETTINGS_PATH = ("Configuración > Sistema > Energía y batería (en algunos Windows se llama «Inicio/apagado y "
                 "batería»)")


@dataclass(frozen=True)
class PowerSnapshot:
    """Lo que dice Windows. None = no se pudo leer (nunca se inventa un valor). Plazos en segundos, 0 = nunca."""

    on_battery: bool | None
    battery_percent: int | None
    has_battery: bool
    lid_present: bool
    modern_standby: bool
    sleep_ac_s: int | None
    sleep_dc_s: int | None
    hibernate_ac_s: int | None
    hibernate_dc_s: int | None
    lid_action_ac: int | None
    lid_action_dc: int | None


def fmt_duration(seconds: int) -> str:
    """«45 s», «3 min», «1 min 30 s», «2 h», «1 h 30 min»."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} h"] if h else []
    if m:
        parts.append(f"{m} min")
    if s and not h:
        parts.append(f"{s} s")
    return " ".join(parts) or "0 s"


def idle_stop(sleep_s: int | None, hibernate_s: int | None, modern_standby: bool) -> tuple[int, str] | None:
    """(segundos, «suspende»|«hiberna») de la primera parada por inactividad; None si nunca o no se sabe.

    Con espera moderna, «hibernar tras» cuenta desde que el PC ya está suspendido: sin suspensión no llega a
    hibernar por inactividad. Sin espera moderna (S3), Windows hiberna tras ese plazo aunque no se suspenda.
    """
    options: list[tuple[int, str]] = []
    if sleep_s:
        options.append((sleep_s, "suspende"))
    if hibernate_s and not modern_standby:
        options.append((hibernate_s, "hiberna"))
    return min(options) if options else None


_STOPPED = {"suspende": "suspendido", "hiberna": "hibernado"}
_SETTING = {"suspende": "la suspensión", "hiberna": "la hibernación"}


def evaluate(snap: PowerSnapshot) -> dict[str, Any]:
    """Cuerpo del campo `power` de `GET /api/status`: los valores y los avisos para la tienda."""
    warnings: list[dict[str, str]] = []
    if snap.on_battery:
        left = f" (queda un {snap.battery_percent} %)" if snap.battery_percent is not None else ""
        stop = idle_stop(snap.sleep_dc_s, snap.hibernate_dc_s, snap.modern_standby)
        if stop is not None:
            secs, verb = stop
            msg = (f"El PC funciona con batería{left} y Windows lo {verb} tras {fmt_duration(secs)} sin usar el "
                   f"teclado ni el ratón: mientras está {_STOPPED[verb]} no se graba ninguna cámara. "
                   "Qué hacer: enchúfalo a la corriente.")
        else:
            msg = (f"El PC funciona con batería{left}: cuando se agote, se apagará y dejará de grabar. "
                   "Qué hacer: enchúfalo a la corriente.")
        warnings.append({"code": "on_battery", "message_es": msg})
    stop = idle_stop(snap.sleep_ac_s, snap.hibernate_ac_s, snap.modern_standby)
    if stop is not None:
        secs, verb = stop
        warnings.append({"code": "idle_sleep_ac", "message_es": (
            f"Con corriente, Windows {verb} este PC tras {fmt_duration(secs)} sin usar el teclado ni el ratón, y "
            f"mientras está {_STOPPED[verb]} no se graba ninguna cámara. Qué hacer: en {SETTINGS_PATH}, pon "
            f"{_SETTING[verb]} en «Nunca» cuando esté enchufado.")})
    if snap.lid_present and snap.lid_action_ac in _LID_DOES:
        warnings.append({"code": "lid_ac", "message_es": (
            f"Al cerrar la tapa, Windows {_LID_DOES[snap.lid_action_ac]} y deja de grabar. Qué hacer: en Panel de "
            "control > Opciones de energía > Elegir el comportamiento del cierre de la tapa, pon «No hacer nada» "
            "cuando esté enchufado.")})
    return {
        "on_battery": snap.on_battery, "battery_percent": snap.battery_percent, "has_battery": snap.has_battery,
        "lid_present": snap.lid_present, "modern_standby": snap.modern_standby,
        "sleep_ac_s": snap.sleep_ac_s, "sleep_dc_s": snap.sleep_dc_s,
        "hibernate_ac_s": snap.hibernate_ac_s, "hibernate_dc_s": snap.hibernate_dc_s,
        "lid_action_ac": LID_ACTIONS.get(snap.lid_action_ac) if snap.lid_action_ac is not None else None,
        "lid_action_dc": LID_ACTIONS.get(snap.lid_action_dc) if snap.lid_action_dc is not None else None,
        "ok": not warnings, "warnings": warnings,
    }


_failure_logged = False   # el fallo de lectura ya está en vms.log (hasta que una lectura vuelva a ir bien)


def power_status() -> dict[str, Any] | None:
    """`evaluate(read_snapshot())`; None fuera de Windows. Un fallo al leer nunca tumba `/api/status`.

    `/api/status` llama aquí cada 5 s por cada página de estado abierta: un fallo que se repite se registra una
    sola vez con su traza (las siguientes, en depuración) y se vuelve a registrar si reaparece tras ir bien."""
    global _failure_logged
    try:
        snap = read_snapshot()
    except Exception:  # noqa: BLE001 - leer la energía es informativo
        if _failure_logged:
            log.debug("Sigue sin poder leerse la configuración de energía del PC", exc_info=True)
        else:
            _failure_logged = True
            log.exception("No se pudo leer la configuración de energía del PC")
        return None
    _failure_logged = False
    return evaluate(snap) if snap is not None else None


def read_snapshot() -> PowerSnapshot | None:
    """Valores reales de este PC (solo lectura). None fuera de Windows."""
    if sys.platform == "win32":  # pragma: no cover - solo Windows
        return _read_windows()
    return None


if sys.platform == "win32":  # pragma: no cover - solo Windows (tests/ops/test_power.py lo lee de verdad)
    import ctypes
    import uuid
    import winreg
    from ctypes import wintypes
    from functools import lru_cache

    class _PowerStatus(ctypes.Structure):
        # SYSTEM_POWER_STATUS
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    class _BatteryScale(ctypes.Structure):
        _fields_ = [("Granularity", wintypes.DWORD), ("Capacity", wintypes.DWORD)]

    # SYSTEM_POWER_CAPABILITIES (76 bytes): BOOLEAN y BYTE de 1 byte, después enteros alineados a 4
    _CAPABILITY_FIELDS: list[tuple[str, Any]] = [(name, ctypes.c_ubyte) for name in (
        "PowerButtonPresent", "SleepButtonPresent", "LidPresent", "SystemS1", "SystemS2", "SystemS3", "SystemS4",
        "SystemS5", "HiberFilePresent", "FullWake", "VideoDimPresent", "ApmPresent", "UpsPresent", "ThermalControl",
        "ProcessorThrottle", "ProcessorMinThrottle", "ProcessorMaxThrottle", "FastSystemS4", "Hiberboot",
        "WakeAlarmPresent", "AoAc", "DiskSpinDown", "HiberFileType", "AoAcConnectivitySupported")]
    _CAPABILITY_FIELDS += [
        ("spare3", ctypes.c_ubyte * 6), ("SystemBatteriesPresent", ctypes.c_ubyte),
        ("BatteriesAreShortTerm", ctypes.c_ubyte), ("BatteryScale", _BatteryScale * 3),
        ("AcOnLineWake", ctypes.c_int), ("SoftLidWake", ctypes.c_int), ("RtcWake", ctypes.c_int),
        ("MinDeviceWakeState", ctypes.c_int), ("DefaultLowLatencyWake", ctypes.c_int)]

    class _Capabilities(ctypes.Structure):
        _fields_ = _CAPABILITY_FIELDS

    class _Guid(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def _guid(text: str) -> _Guid:
        return _Guid.from_buffer_copy(uuid.UUID(text).bytes_le)

    @lru_cache(maxsize=1)
    def _api() -> tuple[Any, Any]:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
        kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_PowerStatus)]
        kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        powrprof.GetPwrCapabilities.argtypes = [ctypes.POINTER(_Capabilities)]
        powrprof.GetPwrCapabilities.restype = ctypes.c_ubyte
        powrprof.PowerGetActiveScheme.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_Guid))]
        powrprof.PowerGetActiveScheme.restype = wintypes.DWORD
        for name in ("PowerReadACValueIndex", "PowerReadDCValueIndex"):
            fn = getattr(powrprof, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Guid), ctypes.POINTER(_Guid), ctypes.POINTER(_Guid),
                           ctypes.POINTER(wintypes.DWORD)]
            fn.restype = wintypes.DWORD
        return kernel32, powrprof

    def _policy_index(setting: str, ac: bool) -> int | None:
        """Valor fijado por directiva de grupo, si lo hay (manda sobre el plan)."""
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{POLICY_KEY}\\{setting.upper()}") as key:
                value = winreg.QueryValueEx(key, "ACSettingIndex" if ac else "DCSettingIndex")[0]
        except OSError:
            return None
        return value if isinstance(value, int) else None

    def _read_index(powrprof: Any, scheme: Any, sub: str, setting: str, ac: bool) -> int | None:
        policy = _policy_index(setting, ac)
        if policy is not None:
            return policy
        if scheme is None:
            return None
        fn = powrprof.PowerReadACValueIndex if ac else powrprof.PowerReadDCValueIndex
        value = wintypes.DWORD()
        rc = fn(None, scheme, ctypes.byref(_guid(sub)), ctypes.byref(_guid(setting)), ctypes.byref(value))
        if rc != 0:
            log.debug("No se pudo leer el ajuste de energía %s (%s): código %s", setting, "AC" if ac else "DC", rc)
            return None
        return int(value.value)

    def _read_windows() -> PowerSnapshot:
        kernel32, powrprof = _api()
        status = _PowerStatus()
        has_status = bool(kernel32.GetSystemPowerStatus(ctypes.byref(status)))
        caps = _Capabilities()
        has_caps = bool(powrprof.GetPwrCapabilities(ctypes.byref(caps)))
        if has_caps:
            has_battery = bool(caps.SystemBatteriesPresent)
        else:  # BatteryFlag 128 = sin batería, 255 = no se sabe
            has_battery = has_status and status.BatteryFlag not in (128, 255)
        on_battery = {0: True, 1: False}.get(status.ACLineStatus) if has_status else None
        percent = status.BatteryLifePercent if has_status and has_battery and status.BatteryLifePercent <= 100 else None

        active = ctypes.POINTER(_Guid)()
        rc = powrprof.PowerGetActiveScheme(None, ctypes.byref(active))
        scheme = active if rc == 0 and active else None
        if scheme is None:
            log.debug("No se pudo leer el plan de energía activo: código %s", rc)
        try:
            def read(sub: str, setting: str, ac: bool) -> int | None:
                return _read_index(powrprof, scheme, sub, setting, ac)

            return PowerSnapshot(
                on_battery=on_battery, battery_percent=percent, has_battery=has_battery,
                lid_present=bool(caps.LidPresent) if has_caps else False,
                modern_standby=bool(caps.AoAc) if has_caps else False,
                sleep_ac_s=read(SUB_SLEEP, STANDBYIDLE, True), sleep_dc_s=read(SUB_SLEEP, STANDBYIDLE, False),
                hibernate_ac_s=read(SUB_SLEEP, HIBERNATEIDLE, True),
                hibernate_dc_s=read(SUB_SLEEP, HIBERNATEIDLE, False),
                lid_action_ac=read(SUB_BUTTONS, LIDACTION, True), lid_action_dc=read(SUB_BUTTONS, LIDACTION, False))
        finally:
            if scheme is not None:
                kernel32.LocalFree(ctypes.cast(active, ctypes.c_void_p))
