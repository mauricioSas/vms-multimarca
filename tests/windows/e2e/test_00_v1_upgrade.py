"""Criterio 3 de B3: instalar el Setup 2.0 encima de una v1 de ``install.ps1`` conserva configuración y grabaciones.

Se instala la v1 de verdad (``deploy/windows/install.ps1``: Python embebible, dependencias, MediaMTX y WinSW), se
crea el administrador y se cambia la sede por la API, se dejan grabaciones de prueba y se instala la v2 encima en
silencio. Con los dobles, ``vmsctl migrate-from-v1`` para y elimina los servicios WinSW (lo que hará el de B1).
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import httpx
import pytest
from dotenv import dotenv_values

from tests.windows import harness as h
from tests.windows.e2e.common import assert_clean_exit, calls, write_inf
from tests.windows.conftest import E2E, ROOT

pytestmark = [pytest.mark.e2e]

V1_PASSWORD = "v1-admin-e2e"


@pytest.mark.paso("v1", "Setup 2.0 encima de una v1 de install.ps1: configuración y grabaciones intactas")
def test_upgrade_from_v1(e2e: E2E, step: h.Step) -> None:
    if os.environ.get("VMS_TEST_WIN_V1") != "1":
        pytest.skip("VMS_TEST_WIN_V1 no está activado (run_e2e --with-v1)")
    step.details["limpieza"] = h.force_clean(e2e.env)

    # --- 1. v1 real
    v1_log = e2e.env.log_path("v1-install-ps1")
    started = time.monotonic()
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "deploy" / "windows" / "install.ps1"), "-Components", "Backend", "-PythonMode", "Embedded",
         "-SiteId", "site-v1-e2e"],
        cwd=ROOT, capture_output=True, timeout=1800, check=False)
    v1_log.write_bytes(proc.stdout + b"\n--- stderr ---\n" + proc.stderr)
    step.details["v1_install_s"] = round(time.monotonic() - started, 1)
    assert proc.returncode == 0, f"install.ps1 de la v1 falló ({proc.returncode}); ver {v1_log}"
    assert h.service_exists("VMSBackend")

    base = "http://127.0.0.1:8600"
    assert h.wait_until(lambda: _health(base), 180, 2), "el backend de la v1 no responde"
    with httpx.Client(base_url=base, timeout=20) as c:
        r = c.post("/api/auth/setup", json={"username": "admin", "password": V1_PASSWORD})
        assert r.status_code == 201, r.text
        r = c.patch("/api/settings", json={"site": {"name": "Tienda v1 e2e", "code": "V1-07"}})
        assert r.status_code == 200, r.text

    data = h.data_dir()
    rec = data / "recordings" / "v1cam_main"
    rec.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (rec / f"2026-10-05_10-0{i}-00-000000+0200.mp4").write_bytes(os.urandom(4096 + i))
    before = {
        "config": json.loads((data / "config" / "config.json").read_text(encoding="utf-8")),
        "users": json.loads((data / "config" / "users.json").read_text(encoding="utf-8")),
        "env": dotenv_values(data / ".env"),
        "recordings": h.tree_hashes(data / "recordings"),
    }
    assert before["config"]["settings"]["site"]["name"] == "Tienda v1 e2e"

    # --- 2. Setup 2.0 encima, en silencio y sin secretos (ya hay usuarios)
    inf = write_inf(e2e.work / "v1.inf", {"SetupType": "control", "Tasks": ""})
    first = len(calls(e2e))
    res = h.run_setup(e2e.installer, e2e.env, ["/TYPE=control", f"/LOADINF={inf}", "/MINFREEGB=1"], "v2-sobre-v1")
    text = assert_clean_exit(res)
    assert "Instalación v1 (install.ps1) detectada" in text

    cmds = [h.command_of(c["argv"]) for c in calls(e2e)[first:]]
    step.details["vmsctl"] = cmds
    assert h.is_subsequence(["migrate-from-v1", "version switch", "services install", "services start"], cmds)
    assert "ports check" not in cmds, "con la v1 instalada sus puertos están en uso: no se comprueban"

    # --- 3. datos intactos
    after_config = json.loads((data / "config" / "config.json").read_text(encoding="utf-8"))
    assert after_config["settings"]["site"]["name"] == "Tienda v1 e2e"
    assert after_config["settings"]["site"]["code"] == "V1-07"
    assert {u["username"] for u in json.loads((data / "config" / "users.json").read_text(encoding="utf-8"))["users"]} \
        == {u["username"] for u in before["users"]["users"]}
    assert h.tree_hashes(data / "recordings") == before["recordings"], "las grabaciones cambiaron"
    env_after = dotenv_values(data / ".env")
    for key in ("VMS_KIOSK_TOKEN", "VMS_SITE_ID", "VMS_CREDENTIAL_BACKEND"):
        assert env_after.get(key) == before["env"].get(key), key
    assert env_after.get("VMS_ENGINE_MODE") == "attach"
    assert "VMS_ADMIN_INITIAL_PASSWORD" not in env_after, "con usuarios no se pide ni guarda contraseña inicial"

    # --- 4. programa: v2 en su sitio, restos de la v1 fuera
    prog = h.program_dir()
    assert (prog / "bin" / "vmshost.exe").is_file()
    assert (prog / "versions" / e2e.version / "bin" / "vmsctl.exe").is_file()
    for gone in ("python", "services", "vms", "deploy"):
        assert not (prog / gone).exists(), f"queda {gone} de la v1"
    if e2e.doubles:
        assert not h.service_exists("VMSBackend"), "el doble de migrate-from-v1 elimina los servicios WinSW"
        step.note("vmsctl de prueba: migrate-from-v1 solo para y elimina los servicios WinSW; el de B1 los "
                  "convertirá a vmshost.")
    else:
        image = h.reg_get(r"SYSTEM\CurrentControlSet\Services\VMSBackend", "ImagePath") or ""
        assert "vmshost.exe" in image.lower()
    assert h.reg_get(h.VMS_KEY, "InstalledVersion") == e2e.version

    # --- 5. dejar el equipo limpio para el resto
    h.run_uninstall(e2e.env, ["/PURGE"], "v1-limpieza")
    step.details["limpieza_final"] = h.force_clean(e2e.env)


def _health(base: str) -> bool:
    try:
        return httpx.get(f"{base}/api/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False
