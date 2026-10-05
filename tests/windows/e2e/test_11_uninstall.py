"""Paso 11 (PLAN-V2 §4.6): desinstalar conservando datos, reinstalar encima (con aviso de Windows 10 simulado),
desinstalar con /PURGE y los códigos de salida de error documentados (puerto ocupado, sistema que no responde)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.windows import harness as h
from tests.windows.e2e.common import assert_clean_exit, calls, commands_since, new_secrets, store_inf
from tests.windows.conftest import E2E

pytestmark = [pytest.mark.e2e]


@pytest.mark.paso("11a", "Desinstalar en silencio: fuera programa, servicios y reglas; se conservan los datos")
def test_step11a_uninstall_keeps_data(e2e: E2E, step: h.Step) -> None:
    if not e2e.state.get("installed"):
        pytest.skip("no hay instalación (falló el paso 2)")
    data = h.data_dir()
    rec: Path = e2e.state["rec"]
    sample = rec / "camara1" / "2026-10-05_10-00-00-000000+0200.mp4"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_bytes(b"\0" * 2048)
    e2e.state["kept"] = {"config": h.sha256(data / "config" / "config.json"), "env": h.sha256(data / ".env"),
                         "sample": h.sha256(sample)}
    first = len(calls(e2e))
    res = h.run_uninstall(e2e.env, [], "desinstalar-conservar")
    assert res.code == 0, res.text()[-4000:]
    cmds = commands_since(e2e, first)
    step.details["vmsctl"] = cmds
    assert h.is_subsequence(["services stop", "services uninstall", "firewall remove"], cmds)
    uninstall_call = next(c for c in calls(e2e)[first:] if h.command_of(c["argv"]) == "services uninstall")
    assert "--purge" not in uninstall_call["argv"]
    assert not (h.program_dir() / "versions").exists() and not (h.program_dir() / "bin").exists()
    assert h.reg_get(h.UNINSTALL_KEY, "DisplayVersion") is None
    assert not h.reg_key_exists(h.VMS_KEY)
    assert h.sha256(data / "config" / "config.json") == e2e.state["kept"]["config"]
    assert h.sha256(sample) == e2e.state["kept"]["sample"], "las grabaciones se conservan"
    assert (data / ".env").is_file() and h.group_exists(h.OPERATORS_GROUP)
    e2e.state["installed"] = False


@pytest.mark.paso("11b", "Reinstalar encima de los datos conservados (y aviso de Windows 10 simulado)")
def test_step11b_reinstall_over_kept_data(e2e: E2E, step: h.Step) -> None:
    if "kept" not in e2e.state:
        pytest.skip("no se ejecutó la desinstalación conservando datos")
    secrets = new_secrets(e2e, "reinstalar-secrets.json")
    res = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", f"/SECRETS={secrets}", "/MINFREEGB=1",
                                               "/SIMULATEWINBUILD=19045"], "reinstalar")
    text = assert_clean_exit(res)
    e2e.state["installed"] = True
    assert "Windows 10" in text and "ESU" in text, "aviso de Windows 10 en el registro (D11)"
    data = h.data_dir()
    assert h.sha256(data / "config" / "config.json") == e2e.state["kept"]["config"], "config.json intacto"
    assert json.loads((data / "state" / "active.json").read_text(encoding="utf-8"))["active"] == e2e.version
    step.note("Sin config.json nuevo: las páginas de grabaciones y sede se saltan y se conserva lo anterior.")


@pytest.mark.paso("11c", "Desinstalar con /PURGE: todo eliminado (datos, grabaciones creadas, grupo)")
def test_step11c_purge(e2e: E2E, step: h.Step) -> None:
    if not e2e.state.get("installed"):
        pytest.skip("no hay instalación")
    rec: Path = e2e.state["rec"]
    first = len(calls(e2e))
    res = h.run_uninstall(e2e.env, ["/PURGE"], "desinstalar-purge")
    assert res.code == 0, res.text()[-4000:]
    uninstall_call = next(c for c in calls(e2e)[first:] if h.command_of(c["argv"]) == "services uninstall")
    assert "--purge" in uninstall_call["argv"]
    assert not h.data_dir().exists(), "ProgramData\\VMSMultimarca tiene que desaparecer con /PURGE"
    assert not rec.exists(), "la carpeta de grabaciones la creó el instalador: se borra"
    assert not h.program_dir().exists() or not any(h.program_dir().iterdir())
    assert not h.group_exists(h.OPERATORS_GROUP)
    e2e.state["installed"] = False


@pytest.mark.paso("11d", "Errores en silencio: secretos que faltan (1), disco pequeño y puerto ocupado (7), "
                         "sistema que no responde (12)")
def test_step11d_error_exit_codes(e2e: E2E, step: h.Step) -> None:
    h.force_clean(e2e.env)
    inf, _ = store_inf(e2e, "errores.inf")
    missing = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", f"/LOADINF={inf}",
                                                   f"/SECRETS={e2e.work / 'no-existe.json'}"], "sin-secretos")
    assert missing.code == 1, missing.code
    small = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", f"/LOADINF={inf}",
                                                 f"/SECRETS={new_secrets(e2e, 'e1.json')}", "/MINFREEGB=999999"],
                        "disco-pequeno")
    assert small.code == 7 and "999999 GB" in small.text(), small.text()[-3000:]
    busy = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", f"/LOADINF={inf}",
                                                f"/SECRETS={new_secrets(e2e, 'e2.json')}", "/MINFREEGB=1"],
                       "puerto-ocupado", extra_env={"VMS_FAKE_VMSCTL_FAIL": "ports-check=10"})
    assert busy.code == 7 and "puerto ocupado" in busy.text(), busy.text()[-3000:]
    if not e2e.doubles:
        step.note("Los fallos simulados necesitan el vmsctl de prueba: el resto de este paso se omite en modo real.")
        return
    unhealthy = h.run_setup(e2e.installer, e2e.env, ["/TYPE=store", f"/LOADINF={inf}",
                                                     f"/SECRETS={new_secrets(e2e, 'e3.json')}", "/MINFREEGB=1"],
                            "sin-respuesta", extra_env={"VMS_FAKE_VMSCTL_FAIL": "health-wait=12"})
    assert unhealthy.code == 12, unhealthy.text()[-3000:]
    assert h.reg_get(h.VMS_KEY, "InstalledVersion") == e2e.version, "instalado aunque no responda"
    step.details["codigos"] = {"sin_secretos": missing.code, "disco": small.code, "puerto": busy.code,
                               "sin_respuesta": unhealthy.code}
    h.run_uninstall(e2e.env, ["/PURGE"], "errores-limpieza")
    assert not h.data_dir().exists()
