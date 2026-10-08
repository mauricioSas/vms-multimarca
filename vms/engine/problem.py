"""Por qué no funciona el motor de vídeo en modo `attach` (CONTRATO §6.8 y §13.10).

En modo attach el backend solo sabe si la API de MediaMTX responde. El motivo de una caída lo escriben MediaMTX y
`vmsctl run` en `logs/engine.log` (ya sin credenciales) y `vmsctl run` resume el estado del servicio en
`logs/status-VMSEngine.json`. Aquí se junta todo en un `EngineProblem` {code, detail_es, action_es} que Estado
del sistema enseña con su «Qué hacer:», sin que el usuario tenga que abrir PowerShell:

- `EngineLogCause` interpreta las líneas de engine.log: los eventos `[vmsctl]` (o `[engine-run]` en desarrollo:
  «proceso en marcha», «el proceso terminó con código N», «parado», «no existe …») y los errores del propio
  MediaMTX, no de una cámara: `<fecha> ERR …`, `ERR: …` (configuración rechazada, sin fecha) y `panic:`.
- `read_run_status` lee status-VMSEngine.json y `read_log_tail` el final de engine.log (64 KB como mucho).
- `port_holder` dice qué programa tiene un puerto (psutil). Si no se puede saber con certeza, no se inventa.
- `diagnose` decide el código y los textos.

Códigos: `port_in_use`, `bad_config`, `crash_loop`, `missing_program`, `waiting_config`, `stopped`, `starting`,
`api_auth` y `no_api`. Los formatos de MediaMTX son los de la v1.21.1 (comprobados ejecutándolo): si cambian, el
error se sigue enseñando tal cual (`crash_loop`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vms.core.interfaces import EngineProblem
from vms.core.rtsp import redact
from vms.core.settings import VmsSettings

from .mtx_config import split_address

log = logging.getLogger("vms.engine.problem")

STALE_AFTER_S = 60.0        # mismos umbrales que `vmsctl` (native/vmsctl/src/runstatus.rs)
CRASH_WINDOW_S = 600.0
MIN_UPTIME_S = 20.0
MAX_STATUS_BYTES = 64 * 1024
LOG_SCAN_BYTES = 64 * 1024
# Un error general de MediaMTX es la causa de una caída si sale poco antes de que termine. Uno de hace horas,
# p. ej. un «[playback] …» con el motor sano, no lo es. El «bind» y los arranques cortos van aparte
# (`EngineLogCause._exit_cause`): detrás de ellos MediaMTX escribe líneas por cada cámara o conexión.
NEAR_EXIT_LINES = 8
TEXT_MAX = 300

_EVENT_RE = re.compile(r"^(\S+) \[(?:vmsctl|engine-run)\] (.*)$")
_RUN_RE = re.compile(r"en marcha \(pid (\d+)\)")
_EXIT_RE = re.compile(r"terminó (?:con código |\(código )(-?\d+)")
_TOOK_RE = re.compile(r" tras (\d+) s\b")
_LINE_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (DEB|INF|WAR|ERR) (.*)$")
_PANIC_RE = re.compile(r"^(?:panic|fatal error): ")
_MISSING_RE = re.compile(r"^(?:no existe |no se pudo lanzar )")
BIND_RE = re.compile(r"listen (tcp|udp)[46]? (\S*?):(\d+): bind: (.+)$")


def engine_error(line: str) -> str:
    """Error del propio MediaMTX (no de una cámara) en una línea de su salida, o ""."""
    m = _LINE_RE.match(line)
    if m:
        return m.group(2) if m.group(1) == "ERR" and not m.group(2).startswith("[path ") else ""
    if line.startswith("ERR: ") or _PANIC_RE.match(line):
        return line
    return ""


@dataclass(frozen=True)
class BindError:
    proto: str          # tcp | udp
    host: str           # "" = todas las direcciones
    port: int
    reason: str         # in_use | forbidden | other

    @classmethod
    def parse(cls, msg: str) -> "BindError | None":
        """«listen tcp 127.0.0.1:9998: bind: Only one usage…» → BindError. Se mira la estructura del error de Go,
        no solo el texto. Go pide a Windows el texto en inglés; si no lo tiene, sale en el idioma del sistema o
        como «winapi error #10048»."""
        m = BIND_RE.search(msg)
        if m is None:
            return None
        low = m.group(4).lower()
        if ("only one usage" in low or "address already in use" in low or "10048" in low
                or "un uso de cada direcci" in low):
            reason = "in_use"
        elif ("forbidden by its access permissions" in low or "permission denied" in low or "10013" in low
              or "permisos de acceso" in low):
            reason = "forbidden"   # 10013: otra cuenta de Windows lo tiene, o un rango reservado (Hyper-V/WSL)
        else:
            reason = "other"       # p. ej. 10049: la dirección del .env ya no es de este PC
        return cls(m.group(1), m.group(2).strip("[]"), int(m.group(3)), reason)


def _event_time(stamp: str) -> float:
    """`2026-10-06T09:06:04Z` (vmsctl, UTC) o `2026-10-08T14:55:43` (engine-run, hora local) → segundos Unix."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (ValueError, OverflowError, OSError):
        return time.time()


@dataclass(frozen=True)
class LogCause:
    """Foto de `EngineLogCause` (para usarla fuera del bucle de eventos)."""
    state: str = ""            # "" (nada visto), running, exited, stopped, waiting o missing
    state_unix: float = 0.0
    exit_code: int | None = None
    exit_error: str = ""       # error del último arranque que terminó ("" si no dejó ninguno)
    missing: str = ""
    exits_unix: tuple[float, ...] = ()
    pid: int | None = None     # MediaMTX en marcha según el último «en marcha (pid N)» (None si ya terminó)

    def crashes(self, now: float) -> int:
        return sum(1 for t in self.exits_unix if now - t < CRASH_WINDOW_S)


class EngineLogCause:
    """Lo que dice engine.log del último arranque del motor. Se alimenta línea a línea (LogTail) y, al arrancar
    el backend con el motor caído, con el final del archivo (`read_log_tail`)."""

    def __init__(self) -> None:
        self.state = ""
        self.state_unix = 0.0
        self.exit_code: int | None = None
        self.exit_error = ""
        self.missing = ""
        self.pid: int | None = None
        self._run_unix = 0.0        # «en marcha» del arranque en curso (0: no se vio)
        self._run_s: float | None = None   # cuánto duró el último arranque que terminó
        self._bind = ""             # primer «listen …: bind» del arranque en curso
        self._first = ""            # primer error general de MediaMTX del arranque en curso
        self._error = ""            # último error general de MediaMTX del arranque en curso
        self._since_error = 0       # líneas de MediaMTX desde ese error
        self._panic = ""            # primera línea «panic:» del arranque en curso (lo demás es la pila)
        self._exits: deque[float] = deque(maxlen=256)

    def _new_run(self, at: float) -> None:
        self._bind, self._first, self._error, self._panic, self.missing = "", "", "", "", ""
        self._since_error, self._run_unix, self._run_s = 0, at, None

    def _exit_cause(self) -> str:
        """Por qué terminó el arranque.

        - MediaMTX (v1.21.1, comprobado ejecutándolo) se cierra SIEMPRE que no puede abrir un puerto, al arrancar
          o al recargar mediamtx.yml. Entre el «bind» y la salida escribe una o dos líneas por cámara («[RTSP
          source] started/stopped»: las rutas arrancan antes que RTSP, WebRTC y la API) o por conexión abierta
          («[conn …] closed»). Así que el primer «bind» manda, esté a las líneas que esté.
        - Un `panic:` también es siempre la causa.
        - Un arranque corto (< MIN_UPTIME_S) cae por su primer error general: todos los del arranque son fatales.
        - Si no, solo vale un error de justo antes de la salida (no uno de hace horas con el motor sano)."""
        if self._bind or self._panic:
            return self._bind or self._panic
        if self._first and self._run_s is not None and self._run_s < MIN_UPTIME_S:
            return self._first
        return self._error if self._since_error <= NEAR_EXIT_LINES else ""

    def feed(self, line: str) -> None:
        line = line.strip()
        ev = _EVENT_RE.match(line)
        if ev:
            at, msg = _event_time(ev.group(1)), ev.group(2)
            if (run := _RUN_RE.search(msg)) is not None:
                self._new_run(at)
                self.pid = int(run.group(1))
                self.state, self.state_unix = "running", at
            elif (ex := _EXIT_RE.search(msg)) is not None:
                took = _TOOK_RE.search(msg)
                self.exit_code = int(ex.group(1))
                self._run_s = float(took.group(1)) if took else (at - self._run_unix if self._run_unix else None)
                self.exit_error = self._exit_cause()
                self._exits.append(at)
                self.pid = None
                self.state, self.state_unix = "exited", at
                # Lo del arranque se conserva hasta el siguiente «en marcha»: vmsctl copia la salida de MediaMTX
                # desde otro hilo y su última línea puede llegar después de «terminó» (ver abajo)
            elif msg == "parado":
                self.pid = None
                self.state, self.state_unix = "stopped", at
            elif msg.startswith("espero a que exista"):
                self.state, self.state_unix = "waiting", at
            elif _MISSING_RE.match(msg):
                self.missing = msg
                self.state, self.state_unix = "missing", at
            return
        self._since_error += 1
        err = engine_error(line)
        if not err:
            return
        if _PANIC_RE.match(err):
            self._panic = self._panic or err
        else:
            # Solo el de los puertos del propio MediaMTX (sin «[componente]» delante), no uno de una sesión
            if not self._bind and err.startswith("listen ") and BindError.parse(err) is not None:
                self._bind = err
            self._first = self._first or err
            self._error, self._since_error = err, 0
        if self.state == "exited":
            self.exit_error = self._exit_cause()   # error rezagado: es del arranque que acaba de terminar

    def snapshot(self) -> LogCause:
        return LogCause(self.state, self.state_unix, self.exit_code, self.exit_error, self.missing,
                        tuple(self._exits), self.pid)


# ------------------------------------------------------------------------------------ archivos de vmsctl
@dataclass(frozen=True)
class RunStatus:
    """`logs/status-VMSEngine.json` (CONTRATO §14; lo escribe `vmsctl run` cada 15 s y en cada cambio)."""
    state: str                       # waiting, idle, running, backoff, failed o stopped
    detail: str = ""
    started_unix: float | None = None
    crashes_unix: tuple[float, ...] = ()
    updated_unix: float = 0.0
    child_pid: int | None = None     # MediaMTX que vmsctl tiene en marcha

    def crashes(self, now: float) -> int:
        return sum(1 for t in self.crashes_unix if now - t < CRASH_WINDOW_S)


def _number(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pid(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def read_run_status(path: Path) -> tuple[RunStatus | None, str]:
    """(estado, "") o (None, motivo): "missing", "denied" o "invalid". Lee 64 KB como mucho."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_STATUS_BYTES + 1)
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        return None, "denied"
    except OSError:
        return None, "invalid"
    if len(raw) > MAX_STATUS_BYTES:
        return None, "invalid"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "invalid"
    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
        return None, "invalid"
    crashes = data.get("crashes_unix")
    return RunStatus(state=data["state"], detail=str(data.get("detail") or "")[:TEXT_MAX],
                     started_unix=_number(data.get("started_unix")),
                     crashes_unix=tuple(t for t in (_number(c) for c in (crashes if isinstance(crashes, list) else []))
                                        if t is not None),
                     updated_unix=_number(data.get("updated_unix")) or 0.0, child_pid=_pid(data.get("child_pid"))), ""


def read_log_tail(path: Path, max_bytes: int = LOG_SCAN_BYTES, *, end: int | None = None) -> list[str]:
    """Las líneas completas de los últimos `max_bytes` de engine.log antes de la posición `end` (por defecto, el
    final). [] si no se puede leer."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            stop = size if end is None else max(0, min(end, size))
            start = max(0, stop - max_bytes)
            f.seek(start)
            data = f.read(stop - start)
    except OSError as exc:
        log.debug("No se pudo leer el final de %s: %s", path, exc)
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()   # el corte puede caer a mitad de un carácter
    if start > 0 and lines:
        lines = lines[1:]                                           # y la primera línea, a medias
    return [ln for ln in lines if ln.strip()]


# ------------------------------------------------------------------------------------ quién tiene el puerto
@dataclass(frozen=True)
class PortHolder:
    name: str                  # nombre del programa, p. ej. «SYS.exe»
    pid: int | None = None


def port_holder(port: int, proto: str = "tcp", host: str = "") -> PortHolder | None:
    """Programa que escucha en `port` en `host`, 0.0.0.0 o :: (`host` vacío = cualquier dirección).

    None si no se puede saber con certeza: psutil sin permiso, proceso ya cerrado, varios programas distintos o
    ninguno. Nunca se adivina. Sin administrador, Windows da el pid y el nombre (no la ruta) de los procesos de
    otras cuentas, como el servicio SYS de HikCentral."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil es dependencia del extra [vms]
        return None
    try:
        conns = psutil.net_connections(kind="udp" if proto == "udp" else "tcp")
    except (psutil.Error, OSError) as exc:
        log.debug("No se pudo saber quién usa el puerto %s: %s", port, exc)
        return None
    hosts = {host, "0.0.0.0", "::"}
    pids: set[int] = set()
    for c in conns:
        laddr = c.laddr
        if not laddr or laddr.port != port or not c.pid:
            continue
        if proto != "udp" and c.status != psutil.CONN_LISTEN:
            continue
        if host and laddr.ip not in hosts:
            continue
        pids.add(c.pid)
    names: set[str] = set()
    for pid in pids:
        try:
            names.add(psutil.Process(pid).name())
        except (psutil.Error, OSError) as exc:   # AccessDenied, NoSuchProcess…: mejor no decir nada
            log.debug("No se pudo saber el nombre del proceso %s: %s", pid, exc)
            return None
    if len(names) != 1:
        return None
    name = names.pop().strip()
    if not name:
        return None
    return PortHolder(name=name[:80], pid=next(iter(pids)) if len(pids) == 1 else None)


HolderLookup = Callable[[int, str, str], "PortHolder | None"]


# ------------------------------------------------------------------------------------ diagnóstico
def port_roles(settings: VmsSettings) -> dict[int, str]:
    """Puerto → para qué lo usa el motor (el aviso dice qué deja de funcionar)."""
    out: dict[int, str] = {}
    for address, role in ((settings.mtx_api_address, "control del motor"),
                          (settings.mtx_playback_address, "reproducción de grabaciones"),
                          (settings.mtx_rtsp_address, "vídeo interno"),
                          (settings.mtx_webrtc_address, "vista en vivo"),
                          (settings.mtx_webrtc_ice_udp, "vista en vivo"),
                          (settings.mtx_webrtc_ice_tcp, "vista en vivo")):
        try:
            hp = split_address(address)
        except ValueError:
            continue
        if hp is not None:
            out.setdefault(hp[1], role)
    return out


def _clip(text: str) -> str:
    text = " ".join(redact(text).split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


def _tries(crashes: int) -> str:
    if crashes <= 0:
        return ""
    return (" Lo ha intentado 1 vez en los últimos 10 minutos." if crashes == 1
            else f" Lo ha intentado {crashes} veces en los últimos 10 minutos.")


def _problem(code: str, detail: str, action: str) -> EngineProblem:
    return EngineProblem(code=code, detail_es=detail, action_es=action)


def _starting() -> EngineProblem:
    return _problem("starting", "El motor de vídeo está arrancando.", "espera unos segundos; esta página se actualiza "
                    "sola.")


def _port_in_use(bind: BindError, crashes: int, roles: dict[int, str], holder: HolderLookup,
                 own_pid: int | None) -> EngineProblem:
    who = holder(bind.port, bind.proto, bind.host)
    if who is not None and own_pid is not None and who.pid == own_pid:
        # Lo tiene el MediaMTX que vmsctl acaba de relanzar: el otro programa ya lo soltó
        return _starting()
    role = f" ({roles[bind.port]})" if bind.port in roles else ""
    port = f"{bind.port}/UDP" if bind.proto == "udp" else str(bind.port)
    if who is not None:
        detail = (f"El motor de vídeo no puede arrancar porque el programa «{who.name}» de este PC ya usa el "
                  f"puerto {port}{role}.")
        action = (f"si usas «{who.name}», no lo cierres. Reinicia el PC, porque a veces el motor arranca antes y "
                  f"se queda con el puerto. Si vuelve a pasar, avisa a soporte y dile el programa "
                  f"(«{who.name}») y el puerto ({port}).")
    else:
        if bind.reason == "forbidden":
            detail = (f"El motor de vídeo no puede arrancar porque Windows no le deja usar el puerto {port}{role}: "
                      "lo usa otro programa o Windows lo tiene reservado.")
        else:
            detail = (f"El motor de vídeo no puede arrancar porque otro programa de este PC ya usa el puerto "
                      f"{port}{role}.")
        action = ("reinicia el PC, porque a veces el motor arranca antes y se queda con el puerto. Si "
                  f"vuelve a pasar, avisa a soporte y dile el puerto ({port}).")
    return _problem("port_in_use", detail + _tries(crashes), action)


def _crashed(error: str, exit_detail: str, crashes: int, roles: dict[int, str],
             holder: HolderLookup, own_pid: int | None) -> EngineProblem:
    """`error` = lo que dijo MediaMTX al caer; `exit_detail` = lo que dice vmsctl («terminó con código 1…»).
    Un «bind» por otro motivo (p. ej. una dirección que ya no es de este PC) no es «otro programa usa el
    puerto»: se cita tal cual."""
    bind = BindError.parse(error)
    if bind is not None and bind.reason != "other":
        return _port_in_use(bind, crashes, roles, holder, own_pid)
    if error.startswith("ERR: "):
        return _problem("bad_config",
                        f"El motor de vídeo no acepta su configuración: «{_clip(error[5:])}».{_tries(crashes)}",
                        "revisa el último equipo o cámara que cambiaste y guárdalo de nuevo. Si sigue igual, "
                        "reinicia el PC y, si tampoco se arregla, avisa a soporte con este mensaje.")
    if error:
        last = f" Último error: «{_clip(error)}»."
    else:
        last = " No ha dejado ningún mensaje de error" + (f" ({_clip(exit_detail)})." if exit_detail else ".")
    return _problem("crash_loop", f"El motor de vídeo se ha cerrado por un error.{_tries(crashes)}{last}",
                    "espera un minuto mientras se reintenta solo. Si sigue igual, reinicia el PC y, si "
                    "tampoco se arregla, avisa a soporte con este mensaje.")


def diagnose(*, api_ok: bool, api_status: int | None, run: RunStatus | None, cause: LogCause, now: float,
             roles: dict[int, str] | None = None, holder: HolderLookup = port_holder) -> EngineProblem | None:
    """El problema del motor, o None si la API responde. `run` = status-VMSEngine.json (None si no hay),
    `cause` = lo visto en engine.log, `api_status` = código HTTP con el que falló la API (None = no contesta)."""
    if api_ok:
        return None
    roles = roles or {}
    stopped = _problem("stopped", "El servicio del motor de vídeo (VMSEngine) está parado: no hay vista en vivo ni "
                       "grabación.", "reinicia el PC. Si al volver sigue parado, avisa a soporte.")
    if run is not None:
        stale = now - run.updated_unix > STALE_AFTER_S
        if run.state == "stopped" or (stale and not cause.crashes(now)):
            return stopped
        if stale:
            run = None   # resumen viejo (p. ej. PC suspendido) pero engine.log tiene caídas recientes: manda el log
    if run is not None:
        state, since, crashes = run.state, run.started_unix, run.crashes(now)
        missing = _MISSING_RE.match(run.detail) is not None and run.state == "failed"
        own_pid = run.child_pid if run.state == "running" else None
    else:
        # Sin status-VMSEngine.json (vmsctl antiguo, sin permiso o dañado): solo engine.log
        if cause.state == "stopped":
            return stopped
        state, since, crashes = cause.state, cause.state_unix, cause.crashes(now)
        if state == "exited" and not crashes:
            state = ""   # una caída de hace más de 10 minutos ya no explica nada: vmsctl relanza mucho antes
        missing = cause.state == "missing"
        own_pid = cause.pid if cause.state == "running" else None
    if state == "waiting":
        return _problem("waiting_config", "El motor de vídeo está esperando su configuración, que escribe VMS "
                        "Multimarca al arrancar.",
                        "espera un minuto; esta página se actualiza sola. Si sigue igual, reinicia el PC.")
    if missing:
        return _problem("missing_program", "Falta el programa del motor de vídeo: la instalación está incompleta (a "
                        "veces lo borra un antivirus).",
                        "vuelve a ejecutar el instalador de VMS Multimarca. Si tienes antivirus, añade la carpeta del "
                        "programa a sus exclusiones.")
    up = now - since if state == "running" and since else None
    if (crashes or state in ("backoff", "failed", "exited")) and not (up is not None and up >= MIN_UPTIME_S):
        return _crashed(cause.exit_error, run.detail if run is not None else "", crashes, roles, holder, own_pid)
    if up is not None and up < MIN_UPTIME_S:
        return _starting()
    if api_status in (401, 403):
        return _problem("api_auth", "El motor de vídeo está en marcha, pero no acepta la clave interna de VMS "
                        "Multimarca.", "reinicia el PC. Si sigue igual, avisa a soporte.")
    return _problem("no_api", "El motor de vídeo (servicio VMSEngine) no responde.",
                    "espera un minuto mientras se reintenta solo. Si no vuelve, reinicia el PC.")


def as_text(p: EngineProblem) -> str:
    """Una sola línea con el motivo y el «Qué hacer:» (para `EngineStatus.last_error` y el registro)."""
    return f"{p.detail_es} Qué hacer: {p.action_es}"
