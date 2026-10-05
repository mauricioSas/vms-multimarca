"""Orquestador de los cortes de luz en Hyper-V (ver `__init__.py`).

El anfitrión habla con Hyper-V con sus cmdlets (`Restore-VMCheckpoint`, `Start-VM`, `Stop-VM -TurnOff`) y con el
invitado por PowerShell Direct (`Invoke-Command -VMName`), sin red entre los dos. Es una herramienta del
laboratorio: el producto no usa PowerShell.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

STATES = ("downloaded", "backed_up", "stopping", "switched", "migrated", "started", "verifying", "good",
          "rolling_back", "rolled_back")
DATA = r"C:\ProgramData\VMSMultimarca"


class VmControl(Protocol):
    def restore(self, checkpoint: str) -> None: ...
    def start(self) -> None: ...
    def power_cut(self) -> None: ...
    def guest(self, script: str, timeout_s: float = 120) -> str: ...


class HyperV:
    """Hyper-V real (anfitrión Windows, consola de administrador)."""

    def __init__(self, vm: str, credential_xml: str | None = None) -> None:
        self.vm = vm
        self.cred = (f"-Credential (Import-Clixml '{credential_xml}')" if credential_xml else "")

    def _ps(self, script: str, timeout_s: float = 300) -> str:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            "$ErrorActionPreference='Stop'; " + script],
                           capture_output=True, timeout=timeout_s, check=False)
        out = p.stdout.decode("utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError(f"PowerShell falló ({p.returncode}): {p.stderr.decode('utf-8', 'replace')[-500:]}")
        return out

    def restore(self, checkpoint: str) -> None:
        self._ps(f"Restore-VMCheckpoint -VMName '{self.vm}' -Name '{checkpoint}' -Confirm:$false")

    def start(self) -> None:
        self._ps(f"Start-VM -Name '{self.vm}'; Wait-VM -Name '{self.vm}' -For Heartbeat -Timeout 600")

    def power_cut(self) -> None:
        self._ps(f"Stop-VM -Name '{self.vm}' -TurnOff -Force")

    def guest(self, script: str, timeout_s: float = 120) -> str:
        wrapped = script.replace("'", "''")
        return self._ps(f"Invoke-Command -VMName '{self.vm}' {self.cred} -ScriptBlock "
                        f"([scriptblock]::Create('{wrapped}'))", timeout_s)


@dataclass
class Observation:
    state: str
    repeat: int
    announced: bool = False
    pointer: dict[str, Any] | None = None
    journal: dict[str, Any] | None = None
    config_valid: bool = False
    services_running: bool = False
    recording: int | None = None
    error: str = ""
    seconds_to_good: float | None = None
    verdict: str = "fail"
    reasons: list[str] = field(default_factory=list)


def judge(o: Observation, *, from_version: str, to_version: str, min_recording: int) -> Observation:
    """Veredicto de una repetición: «nueva buena» o «anterior buena», nunca un estado intermedio."""
    reasons: list[str] = []
    if not o.announced:
        reasons.append("el actualizador no llegó a anunciar el estado (no se cortó donde tocaba)")
    if o.pointer is None:
        reasons.append("active.json ausente o no es JSON")
    else:
        if o.pointer.get("active") not in (from_version, to_version):
            reasons.append(f"versión activa inesperada: {o.pointer.get('active')!r}")
        if o.pointer.get("trial"):
            reasons.append("la versión sigue «a prueba»")
    if o.journal is None:
        reasons.append("journal.json ausente o no es JSON")
    elif o.journal.get("state") not in ("good", "rolled_back"):
        reasons.append(f"el diario quedó en «{o.journal.get('state')}»")
    if not o.config_valid:
        reasons.append("config.json no es JSON válido")
    if not o.services_running:
        reasons.append("hay servicios VMS parados")
    if o.recording is None or o.recording < min_recording:
        reasons.append(f"graban {o.recording} cámaras (se esperaban {min_recording})")
    o.reasons = reasons
    o.verdict = "ok" if not reasons else "fail"
    return o


def _json(text: str) -> dict[str, Any] | None:
    try:
        v = json.loads(text)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def run_one(vm: VmControl, *, checkpoint: str, state: str, repeat: int, source: str, wait_min: float,
            sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
            from_version: str = "2.0.0", to_version: str = "2.0.1", min_recording: int = 8) -> Observation:
    o = Observation(state, repeat)
    vm.restore(checkpoint)
    vm.start()
    # Lanza la comprobación con la pausa en el estado pedido (variables de prueba, solo en builds de prueba)
    vm.guest("[Environment]::SetEnvironmentVariable('VMS_UPDATER_TEST_HOOKS','1','Machine'); "
             f"[Environment]::SetEnvironmentVariable('VMS_UPDATER_PAUSE_AT','{state}','Machine'); "
             f"[Environment]::SetEnvironmentVariable('VMS_UPDATE_SOURCE','{source}','Machine'); "
             "Restart-Service VMSUpdater; Start-Sleep -Seconds 5; "
             # «buscar ahora» con la ventana forzada, por la tubería (en segundo plano: la VM se cortará antes
             # de que responda). Con B1 equivale a «vmsctl update check».
             f"$s = (Get-Content -Raw '{DATA}\\state\\active.json' | ConvertFrom-Json).updater.slot; "
             "Start-Process -WindowStyle Hidden -FilePath \"C:\\Program Files\\VMSMultimarca\\updater\\slot-$s"
             "\\runtime\\python.exe\" -ArgumentList '-m','vms_updater','pipe','{\"cmd\":\"check\",\"force_window\":true}'")
    deadline = monotonic() + 600
    while monotonic() < deadline:
        st = _json(vm.guest(f"Get-Content -Raw '{DATA}\\updater\\public-status.json'"))
        if st and st.get("paused_at") == state:
            o.announced = True
            break
        sleep(2)
    vm.power_cut()
    vm.start()
    t0 = monotonic()
    end = t0 + wait_min * 60
    while True:
        o.journal = _json(vm.guest(f"Get-Content -Raw '{DATA}\\state\\journal.json'"))
        if o.journal and o.journal.get("state") in ("good", "rolled_back") or monotonic() >= end:
            break
        sleep(10)
    o.seconds_to_good = round(monotonic() - t0, 1)
    o.pointer = _json(vm.guest(f"Get-Content -Raw '{DATA}\\state\\active.json'"))
    o.config_valid = _json(vm.guest(f"Get-Content -Raw '{DATA}\\config\\config.json'")) is not None
    svc = vm.guest("(Get-Service VMS* | Where-Object { $_.Status -ne 'Running' } | Measure-Object).Count").strip()
    o.services_running = svc == "0"
    health = _json(vm.guest(
        "$t = Get-Content -Raw '" + DATA + "\\secrets\\internal.token'; "
        "(Invoke-WebRequest -UseBasicParsing -Headers @{'x-vms-internal-token'=$t} "
        "http://127.0.0.1:8600/api/internal/health/deep).Content"))
    o.recording = int(health.get("cameras_recording", 0)) if health else None
    return judge(o, from_version=from_version, to_version=to_version, min_recording=min_recording)


def plan(states: list[str], repeat: int) -> list[tuple[str, int]]:
    unknown = [s for s in states if s not in STATES]
    if unknown:
        raise ValueError(f"estados desconocidos: {unknown}")
    return [(s, i) for s in states for i in range(1, repeat + 1)]


def report(results: list[Observation]) -> dict[str, Any]:
    ok = sum(1 for r in results if r.verdict == "ok")
    return {"date": datetime.now(timezone.utc).isoformat(), "total": len(results), "ok": ok,
            "all_ok": ok == len(results) and bool(results), "runs": [asdict(r) for r in results]}


def markdown(rep: dict[str, Any]) -> str:
    lines = [f"### Cortes de luz en Hyper-V ({rep['date'][:10]}): {rep['ok']}/{rep['total']} bien", "",
             "| Estado | Rep. | Resultado | Versión final | Diario | s hasta terminar | Motivo |",
             "|---|---|---|---|---|---|---|"]
    for r in rep["runs"]:
        lines.append(f"| {r['state']} | {r['repeat']} | {r['verdict']} | {(r['pointer'] or {}).get('active', '—')} | "
                     f"{(r['journal'] or {}).get('state', '—')} | {r['seconds_to_good']} | {'; '.join(r['reasons'])} |")
    return "\n".join(lines) + "\n"


def write(rep: dict[str, Any], out: Path) -> None:
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(rep), encoding="utf-8")
