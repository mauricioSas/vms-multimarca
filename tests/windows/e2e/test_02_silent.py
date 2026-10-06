"""Pasos 1-5 del e2e de Windows (PLAN-V2 §4.6): preparar, instalación silenciosa de una tienda, comprobaciones del
sistema, arranque y visor. Con los dobles de B1/B2, lo que depende de servicios reales se comprueba por el
registro de llamadas de ``vmsctl`` (el contrato) y queda anotado; con los binarios reales, en el propio Windows."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tests.windows import harness as h
from tests.windows.e2e.common import (
    ADMIN_PASSWORD,
    SITE_TOKEN,
    assert_clean_exit,
    calls,
    installed_vmsctl,
    new_secrets,
    store_inf,
)
from tests.windows.conftest import E2E

pytestmark = [pytest.mark.e2e]


@pytest.mark.paso("1", "Preparar: instalador, respuestas (.inf) y secretos; equipo sin VMS Multimarca")
def test_step1_prepare(e2e: E2E, step: h.Step) -> None:
    assert e2e.installer.is_file()
    step.details["instalador"] = str(e2e.installer)
    step.details["modo"] = e2e.mode
    step.details["version"] = e2e.version
    step.details["limpieza"] = h.force_clean(e2e.env)
    inf, rec = store_inf(e2e)
    secrets = new_secrets(e2e)
    e2e.state.update(inf=inf, rec=rec, secrets=secrets)
    assert not h.program_dir().exists() and not h.data_dir().exists()
    if e2e.doubles:
        step.note("Modo dobles: vmshost/vmsctl de prueba (B1) y visor de prueba (B2). camsim y el servidor de "
                  "actualizaciones falso se usan en modo real (pasos 4 y 6-9).")


@pytest.mark.paso("2", "Instalación silenciosa: /VERYSILENT /TYPE=store /LOADINF /SECRETS → código 0")
def test_step2_silent_install(e2e: E2E, step: h.Step) -> None:
    inf, secrets = e2e.state["inf"], e2e.state["secrets"]
    e2e.state["calls_before_install"] = len(calls(e2e))
    res = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", f"/LOADINF={inf}", f"/SECRETS={secrets}",
                                               "/MINFREEGB=1"], "instalacion-silenciosa")
    step.details["segundos"] = round(res.seconds, 1)
    text = assert_clean_exit(res)
    e2e.state["installed"] = True
    e2e.state["install_log"] = text
    assert not Path(secrets).exists(), "el instalador tiene que borrar el archivo de /SECRETS"
    assert ADMIN_PASSWORD not in text and SITE_TOKEN not in text, "secretos en el registro de instalación"
    assert "Secretos leídos de /SECRETS y archivo borrado" in text
    prog = h.program_dir()
    for rel in ("bin/vmshost.exe", f"versions/{e2e.version}/bin/vmsctl.exe", f"versions/{e2e.version}/release.json",
                "updater/slot-a/vmsctl.exe", "uninstall/unins000.exe", "vms.ico"):
        assert (prog / rel).is_file(), rel
    assert h.reg_get(h.VMS_KEY, "InstalledVersion") == e2e.version
    assert h.reg_get(h.UNINSTALL_KEY, "DisplayVersion") == e2e.version
    assert h.reg_get(h.VMS_KEY, "Role") == "store"
    assert h.reg_get(h.VMS_KEY, "DataDir") == str(h.data_dir())


@pytest.mark.paso("2b", "El payload instalado es el de la build reproducible (mismo SHA-256 por archivo)")
def test_step2b_installed_payload_matches_manifest(e2e: E2E, step: h.Step) -> None:
    if e2e.manifest is None or not e2e.manifest.is_file():
        pytest.skip("sin payload-manifest.json de la build (VMS_TEST_WIN_MANIFEST)")
    manifest = json.loads(e2e.manifest.read_text(encoding="utf-8"))
    prog = h.program_dir()
    mismatches = [rel for rel, meta in manifest["files"].items()
                  if not (prog / rel).is_file() or h.sha256(prog / rel) != meta["sha256"]]
    step.details["archivos"] = len(manifest["files"])
    assert not mismatches, f"{len(mismatches)} archivos distintos: {mismatches[:10]}"


@pytest.mark.paso("3", "Comprobaciones del sistema: orden de vmsctl, puntero, .env, config.json, ACL y grupo")
def test_step3_system(e2e: E2E, step: h.Step) -> None:
    data = h.data_dir()
    rec: Path = e2e.state["rec"]
    new_calls = calls(e2e)[e2e.state["calls_before_install"]:]
    cmds = [h.command_of(c["argv"]) for c in new_calls]  # type: ignore[arg-type]
    step.details["vmsctl"] = cmds
    # services install apunta el puntero de versión (sin «version switch» en una instalación nueva, §13.4)
    expected = ["ports check", "services install", "acl apply", "kiosk rotate", "firewall apply",
                "tls setup", "services start", "health wait"]
    assert h.is_subsequence(expected, cmds), f"orden de vmsctl inesperado: {cmds}"
    by_cmd = {h.command_of(c["argv"]): c for c in new_calls}  # type: ignore[arg-type]
    if e2e.doubles:   # con el vmsctl real las órdenes salen del registro de Inno (ver common.calls)
        assert all(c["elevated"] for c in new_calls), "vmsctl tiene que ejecutarse elevado"
        assert all("--json" in c["argv"] for c in new_calls), "el instalador siempre pide --json (§14.2)"
    argv = by_cmd["services install"]["argv"]
    assert h.flag(argv, "role") == "store" and h.flag(argv, "data-dir") == str(data)
    assert h.flag(by_cmd["firewall apply"]["argv"], "profiles") == "private,domain"
    assert h.flag(by_cmd["ports check"]["argv"], "http-port") == "8600"
    assert h.flag(by_cmd["health wait"]["argv"], "timeout") == "120"
    hostname = h.flag(by_cmd["tls setup"]["argv"], "hostname") or ""
    assert hostname == hostname.lower() and hostname

    active = json.loads((data / "state" / "active.json").read_text(encoding="utf-8"))
    assert active["schema"] == 1 and active["active"] == e2e.version

    env = dotenv_values(data / ".env")
    assert env["VMS_ENGINE_MODE"] == "attach"
    assert env["VMS_ADMIN_INITIAL_PASSWORD"] == ADMIN_PASSWORD, "comillas y escapes del .env"
    assert env["VMS_SITE_TOKEN"] == SITE_TOKEN
    assert env["VMS_SITE_ID"] == "e2e-01", "identificador sugerido a partir del código de tienda"
    assert env["VMS_CENTRAL_URL"] == "https://central.e2e.local:8700"
    assert env["VMS_UPDATE_SOURCE"] == "http://127.0.0.1:8765/"
    assert env["VMS_HTTP_PORT"] == "8600" and env["VMS_HTTPS_PORT"] == "8643"
    from vms.core.settings import VmsSettings

    settings = VmsSettings(_env_file=str(data / ".env"))  # type: ignore[call-arg]
    assert settings.engine_mode == "attach" and settings.site_id == "e2e-01"

    from vms.core.models import AppConfig

    raw = (data / "config" / "config.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "config.json sin BOM (json.loads lo rechazaría)"
    cfg = AppConfig.model_validate(json.loads(raw))
    assert cfg.settings.site.name == "Tienda Peñalara e2e", "tildes del .inf en UTF-8"
    assert cfg.settings.site.code == "E2E-01" and cfg.settings.site.id == "e2e-01"
    assert cfg.settings.recording.recordings_dir == str(rec)
    assert (rec / ".vms-multimarca").is_file(), "marca de carpeta creada por el instalador"

    for path in (data / ".env", data / "secrets"):
        sids = h.acl_sids(path)
        step.details[f"acl_{path.name}"] = sids
        assert not {h.SID_USERS, h.SID_AUTH_USERS, h.SID_EVERYONE} & set(sids), f"{path} legible por usuarios"
        assert {h.SID_SYSTEM, h.SID_ADMINS} <= set(sids)

    assert h.group_exists(h.OPERATORS_GROUP)
    members = [m.lower() for m in h.group_member_names(h.OPERATORS_GROUP)]
    assert h.current_user().lower() in members, members

    if e2e.doubles:
        step.note("Servicios, cuentas virtuales, firewall y ACL por SID de servicio: los aplica vmsctl (B1). Con "
                  "los dobles se comprueba el contrato de llamadas; con el vmsctl real, la parte «real» de abajo.")
    else:
        for svc in ("VMSEngine", "VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSUpdater"):
            key = rf"SYSTEM\CurrentControlSet\Services\{svc}"
            assert "vmshost.exe" in (h.reg_get(key, "ImagePath") or "").lower(), svc
            account = h.reg_get(key, "ObjectName") or ""
            assert account == ("LocalSystem" if svc == "VMSUpdater" else rf"NT SERVICE\{svc}"), (svc, account)


@pytest.mark.paso("4", "Arranque: vmsctl health wait (en la instalación y a mano)")
def test_step4_start(e2e: E2E, step: h.Step) -> None:
    proc = subprocess.run([str(installed_vmsctl(e2e)), "health", "wait", "--timeout", "120", "--deep", "--json"],
                          capture_output=True, text=True, timeout=180, check=False)
    step.details["health"] = proc.stdout.strip()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    if e2e.doubles:
        step.note("Con el vmsctl de prueba, health wait responde siempre bien. El alta de 2 NVR con 8 cámaras "
                  "grabando en < 60 s necesita el backend como servicio (B1): se activa en modo real.")
    else:
        import httpx

        assert httpx.get("http://127.0.0.1:8600/api/health", timeout=10).status_code == 200


@pytest.mark.paso("5", "Visor: acceso directo del menú Inicio → vmshost viewer → ventana del visor")
def test_step5_viewer(e2e: E2E, step: h.Step) -> None:
    lnk = Path(h.powershell("[Environment]::GetFolderPath('CommonPrograms')")) / "VMS Multimarca.lnk"
    assert lnk.is_file(), f"falta el acceso directo {lnk}"
    target, args = h.shortcut_target(lnk)
    step.details["acceso_directo"] = [target, args]
    assert Path(target) == h.program_dir() / "bin" / "vmshost.exe" and args == "viewer"
    if not e2e.doubles:
        # Visor real (VMS.exe de B2, sin CDP): el acceso directo lo abre. Su contenido lo prueba el humo por CDP de B2.
        h.powershell("Get-Process VMS -ErrorAction SilentlyContinue | Stop-Process -Force; exit 0")
        proc = subprocess.run([target, args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                              check=False)
        assert proc.returncode == 0, f"vmshost viewer terminó con {proc.returncode}"
        prog = str(h.program_dir()).replace("'", "''")

        def viewer_running() -> bool:
            out = h.powershell(f"Get-Process VMS -ErrorAction SilentlyContinue | "
                               f"Where-Object {{ $_.Path -like '{prog}\\*' }} | ForEach-Object {{ $_.Id }}; exit 0")
            return bool(out.strip())

        assert h.wait_until(viewer_running, 30), "no se abrió el visor VMS.exe"
        step.details["visor"] = "VMS.exe en marcha"
        h.powershell("Get-Process VMS -ErrorAction SilentlyContinue | Stop-Process -Force; exit 0")
        return
    for hwnd in h.find_windows(h.VIEWER_DOUBLE_CLASS):
        h.close_window(hwnd)
    # Sin capturar la salida: el visor que lanza vmshost heredaría las tuberías y run() esperaría a que se cierre.
    proc = subprocess.run([target, args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                          check=False)
    assert proc.returncode == 0, f"vmshost viewer terminó con {proc.returncode}"
    assert h.wait_until(lambda: bool(h.find_windows(h.VIEWER_DOUBLE_CLASS)), 30), "no se abrió el visor"
    hwnds = h.find_windows(h.VIEWER_DOUBLE_CLASS)
    step.details["ventana"] = h.window_title(hwnds[0])
    for hwnd in hwnds:
        h.close_window(hwnd)
    assert h.wait_until(lambda: not h.find_windows(h.VIEWER_DOUBLE_CLASS), 15)
    step.note("Visor de prueba: abre una ventana. Muros por CDP, fotogramas y kiosco: B2 (S1 pendiente).")
    assert sys.platform == "win32"
