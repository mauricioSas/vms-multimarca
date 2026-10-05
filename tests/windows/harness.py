"""Arnés del e2e de Windows (PLAN-V2 §4.6). Dueño: B3; B4 escribe sus pasos 6-9 encima.

Utilidades para lanzar el instalador y el desinstalador, leer el registro de Windows, las ACL y el registro de
llamadas del doble de ``vmsctl``, buscar ventanas y guardar ``results-windows.json`` (mismo formato que
``tests/e2e/results-sistema.json``). Todo lo específico de Windows se importa dentro de cada función: el módulo se
puede importar (y su parte portable probar) en cualquier sistema.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP_ID = "{8D3F0C52-6A1B-4E7C-9B2D-5F4A3C2E1D07}"
UNINSTALL_KEY = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"
VMS_KEY = r"SOFTWARE\VMSMultimarca"
RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
OPERATORS_GROUP = "VMS Operadores"
VIEWER_DOUBLE_CLASS = "VMSVisorDePrueba"
SERVICES = ("VMSEngine", "VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral", "VMSUpdater")
SID_SYSTEM = "S-1-5-18"
SID_ADMINS = "S-1-5-32-544"
SID_USERS = "S-1-5-32-545"
SID_AUTH_USERS = "S-1-5-11"
SID_EVERYONE = "S-1-1-0"


def program_dir() -> Path:
    return Path(os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files") \
        / "VMSMultimarca"


def data_dir() -> Path:
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "VMSMultimarca"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hashes(base: Path) -> dict[str, str]:
    return {p.relative_to(base).as_posix(): sha256(p) for p in sorted(base.rglob("*")) if p.is_file()}


def read_text_any(path: Path) -> str:
    """Registros de Inno (UTF-8 con o sin BOM) y salidas de Windows (a veces UTF-16)."""
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# =============================================================================================== resultados
@dataclass
class Step:
    key: str
    title: str
    ok: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def note(self, text: str) -> None:
        self.notes.append(text)


@dataclass
class Results:
    """``results-windows.json``: el formato de ``tests/e2e/results-sistema.json`` + modo y versión."""
    installer: str
    mode: str
    version: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = ""
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at, "finished_at": self.finished_at,
            "platform": f"{platform.system()} {platform.release()} {platform.version()} {platform.machine()}",
            "installer": self.installer, "mode": self.mode, "version": self.version,
            "summary": {"ok": sum(1 for s in self.steps if s.ok is True),
                        "failed": sum(1 for s in self.steps if s.ok is False),
                        "skipped": sum(1 for s in self.steps if s.ok is None)},
            "steps": [s.__dict__ for s in self.steps],
        }

    def save(self, path: Path) -> None:
        self.finished_at = datetime.now(UTC).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str) + "\n",
                        encoding="utf-8")

    def markdown(self) -> str:
        lines = ["# e2e de Windows — resultados", "",
                 f"Instalador `{Path(self.installer).name}` · modo **{self.mode}** · {self.started_at}", "",
                 "| Paso | Qué se probó | Resultado | s |", "|---|---|---|---|"]
        for s in self.steps:
            mark = {True: "**OK**", False: "**FALLO**", None: "omitido"}[s.ok]
            lines.append(f"| {s.key} | {s.title} | {mark} | {s.seconds:.1f} |")
        for s in self.steps:
            if s.notes:
                lines += ["", f"**{s.key} · {s.title}**", *[f"- {n}" for n in s.notes]]
        return "\n".join(lines) + "\n"


# =============================================================================================== procesos
@dataclass
class RunResult:
    code: int
    log: Path
    seconds: float

    def text(self) -> str:
        return read_text_any(self.log) if self.log.is_file() else ""


class Env:
    """Entorno común de las ejecuciones del e2e (variables de los dobles y carpeta de trabajo)."""

    def __init__(self, work: Path, calls_log: Path, logs: Path | None = None) -> None:
        self.work = work
        self.calls_log = calls_log
        self.logs = logs or work / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self._counter = 0

    def environ(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["VMS_FAKE_VMSCTL_LOG"] = str(self.calls_log)
        env.update(extra or {})
        return env

    def log_path(self, name: str) -> Path:
        self._counter += 1
        return self.logs / f"{self._counter:02d}-{name}.log"


def run_setup(installer: Path, env: Env, args: Sequence[str], name: str, *, timeout: float = 1200,
              extra_env: dict[str, str] | None = None) -> RunResult:
    log = env.log_path(name)
    cmd = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}", *args]
    started = time.monotonic()
    proc = subprocess.run(cmd, env=env.environ(extra_env), timeout=timeout, check=False)
    return RunResult(proc.returncode, log, time.monotonic() - started)


def uninstaller() -> Path | None:
    value = reg_get(UNINSTALL_KEY, "UninstallString")
    if not value:
        return None
    return Path(value.strip().strip('"'))


def run_uninstall(env: Env, args: Sequence[str], name: str, *, timeout: float = 600,
                  extra_env: dict[str, str] | None = None) -> RunResult:
    exe = uninstaller()
    if exe is None or not exe.is_file():
        raise AssertionError(f"No hay desinstalador registrado ({UNINSTALL_KEY})")
    log = env.log_path(name)
    cmd = [str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}", *args]
    started = time.monotonic()
    proc = subprocess.run(cmd, env=env.environ(extra_env), timeout=timeout, check=False)
    # La primera fase del desinstalador espera a la segunda; aun así, se espera a que desaparezcan la clave y el
    # propio desinstalador (lo último que se borra), para no comprobar nada antes de que termine.
    wait_until(lambda: reg_get(UNINSTALL_KEY, "UninstallString") is None and not exe.exists(), 180)
    time.sleep(1)
    return RunResult(proc.returncode, log, time.monotonic() - started)


def windows_powershell_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Entorno para ``powershell.exe`` (5.1) lanzado desde un proceso de PowerShell 7 (el runner de CI): sin el
    ``PSModulePath`` de pwsh, que le hace cargar módulos de la 7 y falla (Get-Acl, Get-LocalGroup…)."""
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if k.upper() == "PSMODULEPATH"]:
        del env[key]
    return env


def powershell(script: str, *, timeout: float = 120) -> str:
    """PowerShell solo en las pruebas (el producto no lo usa). Salida en UTF-8."""
    full = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $ErrorActionPreference = 'Stop'; " + script
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", full],
                          capture_output=True, timeout=timeout, env=windows_powershell_env(), check=False)
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell falló ({proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[-2000:]}")
    return out.strip()


# =============================================================================================== registro
def _winreg() -> Any:
    import winreg

    return winreg


def reg_get(key: str, name: str) -> str | None:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value, _ = winreg.QueryValueEx(k, name)
            return str(value)
    except OSError:
        return None


def reg_set(key: str, name: str, value: str) -> None:
    winreg = _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def reg_delete_value(key: str, name: str) -> None:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def reg_key_exists(key: str) -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            return True
    except OSError:
        return False


def service_exists(name: str) -> bool:
    return reg_key_exists(rf"SYSTEM\CurrentControlSet\Services\{name}")


# =============================================================================================== vmsctl (doble)
def read_calls(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in read_text_any(path).splitlines() if line.strip()]


def command_of(argv: Sequence[str]) -> str:
    """``["services", "install", "--role", "store", ...]`` → ``"services install"``."""
    words = [a for a in argv if not a.startswith("--")]
    if not words:
        return ""
    if words[0] in ("migrate-from-v1", "run"):
        return words[0]
    return " ".join(words[:2])


def flag(argv: Sequence[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == f"--{name}" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def is_subsequence(needle: Iterable[str], haystack: Iterable[str]) -> bool:
    it = iter(haystack)
    return all(any(h == n for h in it) for n in needle)


# =============================================================================================== ACL y grupos
def acl_sids(path: Path) -> dict[str, str]:
    """SID → derechos de la ACL de ``path`` (por SID: sin depender del idioma de Windows)."""
    script = (f"$a = Get-Acl -LiteralPath '{path}'; foreach ($r in $a.Access) {{ "
              "$sid = try { $r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } "
              "catch { $r.IdentityReference.Value }; "
              "Write-Output ($sid + '|' + $r.FileSystemRights + '|' + $r.AccessControlType) }")
    out: dict[str, str] = {}
    for line in powershell(script).splitlines():
        if "|" in line:
            sid, rights, kind = line.split("|", 2)
            if kind.strip() == "Allow":
                out[sid.strip()] = rights.strip()
    return out


def group_exists(name: str) -> bool:
    return powershell(f"if (Get-LocalGroup -Name '{name}' -ErrorAction SilentlyContinue) {{ 'si' }} else {{ 'no' }}") \
        == "si"


def group_member_names(name: str) -> list[str]:
    out = powershell(f"Get-LocalGroupMember -Group '{name}' | ForEach-Object {{ $_.Name }}")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def shortcut_target(lnk: Path) -> tuple[str, str]:
    out = powershell(f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
                     "Write-Output ($s.TargetPath + '|' + $s.Arguments)")
    target, _, args = out.partition("|")
    return target.strip(), args.strip()


def current_user() -> str:
    return f"{os.environ.get('COMPUTERNAME', '')}\\{os.environ.get('USERNAME', '')}"


# =============================================================================================== ventanas
def find_windows(class_name: str) -> list[int]:
    if sys.platform != "win32":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd: int, _: int) -> bool:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
        if buf.value == class_name and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(proto(cb), None)
    return found


def close_window(hwnd: int) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)   # WM_CLOSE


def window_title(hwnd: int) -> str:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 512)
    return buf.value


# =============================================================================================== limpieza
def force_clean(env: Env) -> list[str]:
    """Deja el equipo sin VMS Multimarca (para empezar un escenario desde cero). Devuelve lo que hizo."""
    done = []
    if uninstaller() is not None:
        run_uninstall(env, ["/PURGE"], "limpieza-purge")
        done.append("desinstalación con /PURGE")
    for path in (program_dir(), data_dir()):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            done.append(f"borrada {path}")
    reg_delete_value(RUN_KEY, "VMSMultimarcaMuros")
    for svc in SERVICES:
        if service_exists(svc):
            subprocess.run(["sc.exe", "stop", svc], capture_output=True, check=False)
            subprocess.run(["sc.exe", "delete", svc], capture_output=True, check=False)
            done.append(f"servicio {svc} eliminado")
    return done
