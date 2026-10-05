"""Prueba de B1 en Windows real (job `b1-windows` de CI, runner windows-latest como administrador).

Criterio (4) de PLAN-V2 §6.2: `vmsctl services install --role store` con un payload mínimo → servicios con
`NT SERVICE\\…` e `ImagePath` en `vmshost`, `vmsctl health wait` = 0 y `services uninstall` limpio. Además:
ACL por SID, firewall por puerto, procesos con su cuenta virtual, puertos ocupados (10), vuelta atrás de
una versión a prueba rota **por petición** (solo VMSUpdater escribe el puntero), diag bundle sin secretos,
parada sin huérfanos y migración desde servicios WinSW de la v1.

Payload mínimo: `vmshost.exe`, `versions\\<X>\\bin\\vmsctl.exe`, el runtime embebible con solo
`requirements-vms.txt` (la analítica y el latido no tienen sus dependencias: caen y `vmshost` los relanza
con espera, que también es una prueba), el código (`app\\vms`, `analytics`, `central`) y `mediamtx.exe`.

Solo biblioteca estándar. Las comprobaciones leen el registro y las ACL por SID (nunca texto traducido),
salvo `Get-CimInstance … GetOwner` y `Get-Acl`, que solo usa esta prueba.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import winreg  # type: ignore[import-not-found,unused-ignore]
import zipfile
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
SERVICES = ["VMSEngine", "VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSUpdater"]
DELAYED = {"VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSUpdater"}
VERSION = "2.0.0-ci"
BROKEN = "2.0.1-roto"

results: dict[str, Any] = {}
failed = False


def step(name: str, fn: Callable[[], Any], critical: bool = True) -> Any:
    global failed
    print(f"== {name}", flush=True)
    t0 = time.monotonic()
    try:
        detail = fn()
        results[name] = {"ok": True, "s": round(time.monotonic() - t0, 1), "detail": detail}
        print(f"   OK ({results[name]['s']} s): {detail}", flush=True)
        return detail
    except Exception as exc:  # noqa: BLE001 - se anota y se sigue (o se corta si es crítico)
        failed = True
        results[name] = {"ok": False, "s": round(time.monotonic() - t0, 1), "detail": f"{type(exc).__name__}: {exc}"}
        print(f"   FALLO: {exc}", flush=True)
        traceback.print_exc()
        if critical:
            raise
        return None


def run(cmd: list[str], timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")


class Ctl:
    def __init__(self, exe: Path, data: Path) -> None:
        self.exe, self.data = exe, data

    def __call__(self, *args: str, expect: int | None = 0, timeout: float = 300) -> dict[str, Any]:
        r = run([str(self.exe), *args, "--json", "--data-dir", str(self.data)], timeout)
        line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
        try:
            out: dict[str, Any] = json.loads(line)
        except ValueError:
            out = {"raw": r.stdout[-2000:]}
        out["_exit"] = r.returncode
        if expect is not None and r.returncode != expect:
            raise AssertionError(f"vmsctl {' '.join(args)} → {r.returncode} (esperado {expect}): {line[:1500]} "
                                 f"{r.stderr[-800:]}")
        return out


def ps(script: str) -> str:
    r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], 120)
    if r.returncode != 0:
        raise AssertionError(f"PowerShell falló: {r.stderr[-800:]}")
    return r.stdout.strip()


def reg(key: str, name: str) -> Any:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
        return winreg.QueryValueEx(k, name)[0]


def reg_exists(key: str) -> bool:
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY).Close()
        return True
    except OSError:
        return False


def service_sid(name: str) -> str:
    d = hashlib.sha1(name.upper().encode("utf-16-le"), usedforsecurity=False).digest()
    return "S-1-5-80-" + "-".join(str(int.from_bytes(d[i:i + 4], "little")) for i in range(0, 20, 4))


def acl_sids(path: Path) -> set[str]:
    out = ps(f"(Get-Acl -LiteralPath '{path}').Access | ForEach-Object {{ "
             "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }")
    return {line.strip() for line in out.splitlines() if line.strip()}


def wait_until(cond: Callable[[], bool], timeout: float, what: str) -> float:
    t0 = time.monotonic()
    last: Exception | None = None
    while time.monotonic() - t0 < timeout:
        try:
            if cond():
                return round(time.monotonic() - t0, 1)
        except Exception as exc:  # noqa: BLE001 - se reintenta
            last = exc
        time.sleep(1)
    raise AssertionError(f"tiempo agotado ({timeout} s) esperando: {what} ({last})")


def procs_under(root: Path) -> list[str]:
    out = ps("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath } | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }")
    return [line for line in out.splitlines() if str(root).lower() in line.lower()]


def owner_of(image: str, needle: str = "") -> set[str]:
    out = ps(f"Get-CimInstance Win32_Process -Filter \"Name = '{image}'\" | Where-Object {{ $_.CommandLine -like '*{needle}*' }} | "
             "ForEach-Object { $o = Invoke-CimMethod -InputObject $_ -MethodName GetOwner; \"$($o.Domain)\\$($o.User)\" }")
    return {line.strip() for line in out.splitlines() if line.strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", type=Path, required=True, help="carpeta con vmshost.exe y vmsctl.exe")
    ap.add_argument("--runtime", type=Path, required=True)
    ap.add_argument("--mediamtx", type=Path, required=True)
    ap.add_argument("--root", type=Path, default=Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VMSMultimarca")
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "VMSMultimarca")
    ap.add_argument("--out", type=Path, default=Path("b1-windows.json"))
    ap.add_argument("--logs", type=Path, default=Path("b1-windows-logs"))
    a = ap.parse_args()
    root, data = a.root, a.data
    vdir = root / "versions" / VERSION
    ctl = Ctl(vdir / "bin" / "vmsctl.exe", data)

    def collect_logs(tag: str) -> None:
        dest = a.logs / tag
        for sub in ("logs", "state"):
            src = data / sub
            if src.is_dir():
                shutil.copytree(src, dest / sub, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.tmp-*"))

    def assemble() -> str:
        for p in (root, data):
            if p.exists():
                shutil.rmtree(p)
        (root / "bin").mkdir(parents=True)
        shutil.copy2(a.build / "vmshost.exe", root / "bin" / "vmshost.exe")
        (vdir / "bin").mkdir(parents=True)
        shutil.copy2(a.build / "vmsctl.exe", vdir / "bin" / "vmsctl.exe")
        shutil.copytree(a.runtime, vdir / "runtime")
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        for pkg in ("vms", "analytics", "central"):
            shutil.copytree(REPO / pkg, vdir / "app" / pkg, ignore=ignore)
        (vdir / "engine").mkdir()
        shutil.copy2(a.mediamtx, vdir / "engine" / "mediamtx.exe")
        r = run([str(vdir / "runtime" / "python.exe"), "-c", "import vms, fastapi, cryptography, sys; print(vms.__file__)"])
        assert r.returncode == 0 and str(vdir / "app") in r.stdout, f"runtime embebible: {r.stdout} {r.stderr}"
        return f"payload en {vdir}; el runtime embebible importa vms desde app\\"

    def versions() -> str:
        a1 = run([str(root / "bin" / "vmshost.exe"), "--version"]).stdout.strip()
        a2 = run([str(vdir / "bin" / "vmsctl.exe"), "--version"]).stdout.strip()
        assert a1.startswith("vmshost ") and a2.startswith("vmsctl "), (a1, a2)
        return f"{a1} · {a2}"

    def ports_free() -> str:
        out = ctl("ports", "check", "--role", "store")
        return f"{len(out['data']['ports'])} puertos libres"

    def install() -> str:
        out = ctl("services", "install", "--role", "store", timeout=600)
        assert len(out["data"]["services"]) == 5, out
        return f"servicios: {[s['name'] for s in out['data']['services']]}; ACL: {out['data']['acl_steps']} pasos"

    def check_services() -> dict[str, Any]:
        info = {}
        for s in SERVICES:
            key = rf"SYSTEM\CurrentControlSet\Services\{s}"
            image = reg(key, "ImagePath")
            account = reg(key, "ObjectName")
            want_image = f'"{root / "bin" / "vmshost.exe"}" service --name {s}'
            assert image.lower() == want_image.lower(), f"{s}: ImagePath {image!r} (esperado {want_image!r})"
            want_acc = "LocalSystem" if s == "VMSUpdater" else f"NT SERVICE\\{s}"
            assert account.lower() == want_acc.lower(), f"{s}: cuenta {account!r}"
            assert reg(key, "Start") == 2, f"{s}: inicio automático"
            delayed = reg(key, "DelayedAutostart") if s in DELAYED else 0
            assert bool(delayed) == (s in DELAYED), f"{s}: inicio retrasado {delayed}"
            env = reg(key, "Environment")
            assert f"VMS_DATA_DIR={data}" in env, f"{s}: Environment {env}"
            info[s] = {"account": account, "image": image}
        return info

    def check_recovery_and_sid() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in SERVICES:
            # SERVICE_FAILURE_ACTIONS en el registro: cabecera de 20 bytes + (tipo, espera ms) por acción
            fa = reg(rf"SYSTEM\CurrentControlSet\Services\{s}", "FailureActions")
            reset, count = int.from_bytes(fa[0:4], "little"), int.from_bytes(fa[12:16], "little")
            acts = [(int.from_bytes(fa[20 + 8 * i:24 + 8 * i], "little"),
                     int.from_bytes(fa[24 + 8 * i:28 + 8 * i], "little")) for i in range(count)]
            assert reset == 86400 and acts == [(1, 1000), (1, 5000), (1, 30000)], f"{s}: recuperación {reset} {acts}"
            out[s] = acts
        # El SID calculado (Rust y Python) es el mismo que da Windows
        sc = run(["sc.exe", "showsid", "VMSBackend"]).stdout
        assert service_sid("VMSBackend") in sc, f"SID de servicio distinto: {sc}"
        out["sid_VMSBackend"] = service_sid("VMSBackend")
        return out

    def check_acl() -> dict[str, list[str]]:
        sys_, admins, users, auth = "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545", "S-1-5-11"
        backend, engine = service_sid("VMSBackend"), service_sid("VMSEngine")
        found = {}
        for sub, must, never in [
            ("", {sys_, admins}, {users, auth}),
            ("secrets", {sys_, admins, backend}, {users, auth, engine}),
            ("recordings", {sys_, admins, backend, engine}, {users, auth}),
            ("mediamtx", {sys_, admins, backend, engine}, {users, auth}),
            ("config", {sys_, admins, backend}, {users, auth, engine}),
            ("state", {sys_, admins, backend, engine}, {users, auth}),
        ]:
            sids = acl_sids(data / sub if sub else data)
            assert must <= sids, f"{sub or 'raíz'}: faltan {must - sids} (hay {sids})"
            assert not (never & sids), f"{sub or 'raíz'}: sobran {never & sids}"
            found[sub or "raíz"] = sorted(sids)
        tok = acl_sids(data / "secrets" / "internal.token")
        assert service_sid("VMSAnalytics") in tok and service_sid("VMSHeartbeat") in tok, tok
        key = (data / "secrets" / "secret.key").read_text(encoding="ascii")
        assert key.startswith("vms-dpapi-v1:"), "secret.key protegida con DPAPI de máquina"
        return found

    def firewall() -> str:
        ctl("firewall", "apply", "--profiles", "private", "--role", "store")
        names = ["VMSMultimarca-Web-TCP-8600", "VMSMultimarca-WebRTC-UDP-8189", "VMSMultimarca-WebRTC-TCP-8189"]
        for n in names:
            assert run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={n}"]).returncode == 0, n
        prof = ps(f"(Get-NetFirewallRule -DisplayName '{names[0]}').Profile")
        assert prof.strip() == "Private", f"perfil {prof!r}"
        return f"{names} solo en perfil Privado"

    def start_and_health() -> str:
        ctl("services", "start", timeout=180)
        out = ctl("health", "wait", "--timeout", "240", timeout=300)
        return f"sano en {out['data']['waited_s']} s: {out['data']['checks']}"

    def identities() -> dict[str, Any]:
        mtx = owner_of("mediamtx.exe")
        assert mtx == {"NT SERVICE\\VMSEngine"}, f"mediamtx.exe corre como {mtx}"
        py = owner_of("python.exe", "-m vms")
        assert "NT SERVICE\\VMSBackend" in py, f"python -m vms corre como {py}"
        hosts = owner_of("vmshost.exe")
        assert {"NT SERVICE\\VMSEngine", "NT SERVICE\\VMSBackend"} <= hosts, hosts
        return {"mediamtx": sorted(mtx), "backend": sorted(py), "vmshost": sorted(hosts)}

    def ports_busy() -> str:
        out = ctl("ports", "check", "--role", "store", expect=10)
        busy = [p["port"] for p in out["data"]["ports"] if p["status"] != "free"]
        assert 8600 in busy and 9997 in busy, busy
        return f"código 10; ocupados {busy}"

    def logs_redacted() -> str:
        eng = (data / "logs" / "engine.log").read_text(encoding="utf-8", errors="replace")
        assert "[vmsctl]" in eng and "INF" in eng, eng[-1500:]
        assert (data / "logs" / "VMSBackend.log").is_file()
        yml = (data / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8")
        assert "modo attach" in yml
        return f"engine.log {len(eng)} bytes; mediamtx.yml de engine-config"

    def broken_version_rolls_back_by_request() -> str:
        bdir = root / "versions" / BROKEN
        (bdir / "bin").mkdir(parents=True)
        shutil.copy2(vdir / "bin" / "vmsctl.exe", bdir / "bin" / "vmsctl.exe")   # sin runtime ni motor: rota
        out = ctl("version", "switch", BROKEN)
        assert out["data"]["pointer"]["trial"] is True

        def back() -> bool:
            p = json.loads((data / "state" / "active.json").read_text(encoding="utf-8"))
            return p["active"] == VERSION and not p["trial"] and p["previous"] == BROKEN

        took = wait_until(back, 180, "vuelta atrás de la versión rota")
        rec = json.loads((data / "state" / "host-rollback.json").read_text(encoding="utf-8"))
        assert rec["from"] == BROKEN and rec["to"] == VERSION and rec["by"] == "vmshost", rec
        logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (data / "logs").glob("vmshost-*.log"))
        assert "pido la vuelta atrás" in logs, "un servicio sin privilegios pidió la vuelta atrás"
        assert not list((data / "state" / "requests").glob("rollback-*.json")), "peticiones consumidas"
        out = ctl("health", "wait", "--timeout", "240", timeout=300)
        return f"vuelta atrás en {took} s ({rec['reason']}); sano otra vez en {out['data']['waited_s']} s"

    def diag() -> str:
        z = Path("b1-diag.zip").resolve()
        ctl("diag", "bundle", "--out", str(z))
        token = (data / "secrets" / "internal.token").read_text(encoding="ascii").strip()
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            assert "logs/engine.log" in names and "state/active.json" in names, names
            assert not any("secret" in n or "mediamtx" in n for n in names), names
            for n in names:
                assert token not in zf.read(n).decode("utf-8", errors="replace"), f"{n} contiene el token interno"
        return f"{len(names)} archivos, sin secretos"

    def stop_no_orphans() -> str:
        ctl("services", "stop", timeout=240)
        wait_until(lambda: not procs_under(root), 30, "sin procesos de la instalación")
        return "parados; ningún proceso de Program Files\\VMSMultimarca"

    def uninstall() -> str:
        ctl("services", "uninstall", timeout=240)
        for s in SERVICES:
            wait_until(lambda s=s: not reg_exists(rf"SYSTEM\CurrentControlSet\Services\{s}"), 30, f"{s} borrado")
        assert run(["netsh", "advfirewall", "firewall", "show", "rule", "name=VMSMultimarca-Web-TCP-8600"]).returncode != 0
        assert (data / "config").is_dir() and (data / "secrets" / "secret.key").is_file(), "datos conservados"
        return "servicios y reglas quitados; datos conservados"

    def migrate_v1() -> str:
        for s in ("VMSBackend", "VMSAnalytics", "VMSHeartbeat"):
            winsw = root / "services" / f"{s}.exe"
            r = run(["sc.exe", "create", s, "binPath=", f'"{winsw}"', "start=", "demand"])
            assert r.returncode == 0, r.stdout
        plan = ctl("migrate-from-v1", "--dry-run")
        assert plan["data"]["plan"]["role"] == "store", plan
        out = ctl("migrate-from-v1", timeout=600)
        assert out["data"]["migrated"] is True
        for s in SERVICES:
            key = rf"SYSTEM\CurrentControlSet\Services\{s}"
            assert "vmshost.exe" in reg(key, "ImagePath").lower(), s
        assert reg(r"SYSTEM\CurrentControlSet\Services\VMSBackend", "ObjectName").lower() == "nt service\\vmsbackend"
        ctl("services", "start", timeout=180)
        h = ctl("health", "wait", "--timeout", "240", timeout=300)
        return f"WinSW → vmshost (puesto store); sano en {h['data']['waited_s']} s"

    def purge() -> str:
        ctl("services", "uninstall", "--purge", timeout=240)
        assert not data.exists(), "datos borrados con --purge"
        return "desinstalado con --purge"

    try:
        step("montar_payload", assemble)
        step("versiones", versions)
        step("puertos_libres", ports_free)
        step("services_install_store", install)
        step("servicios_cuentas_virtuales_e_imagepath", check_services)
        step("recuperacion_scm_y_sid_de_servicio", check_recovery_and_sid, critical=False)
        step("acl_por_sid", check_acl, critical=False)
        step("firewall_por_puerto", firewall, critical=False)
        step("arranque_y_health_wait", start_and_health)
        step("procesos_con_su_cuenta", identities, critical=False)
        step("puertos_ocupados_codigo_10", ports_busy, critical=False)
        step("registros", logs_redacted, critical=False)
        step("version_rota_vuelve_atras_por_peticion", broken_version_rolls_back_by_request, critical=False)
        step("diag_bundle_sin_secretos", diag, critical=False)
        collect_logs("antes-de-parar")
        step("parar_sin_huerfanos", stop_no_orphans, critical=False)
        step("services_uninstall_limpio", uninstall)
        step("migrate_from_v1", migrate_v1, critical=False)
        collect_logs("tras-migrar")
        step("uninstall_purge", purge, critical=False)
    except Exception:  # noqa: BLE001 - paso crítico fallido: se limpia y se informa
        pass
    finally:
        collect_logs("final")
        if (vdir / "bin" / "vmsctl.exe").exists():
            run([str(vdir / "bin" / "vmsctl.exe"), "services", "uninstall", "--purge", "--json", "--data-dir", str(data)])
        shutil.rmtree(root, ignore_errors=True)
    results["all_ok"] = not failed
    a.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v["ok"] if isinstance(v, dict) else v for k, v in results.items()}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
