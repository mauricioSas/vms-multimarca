"""Criterio 4 de B3: capturas de cada página del asistente (artefacto de CI), con una instalación interactiva
completa de un puesto de control (muros al iniciar sesión)."""
from __future__ import annotations

import json

import pytest

from tests.windows import harness as h
from tests.windows import wizard_capture
from tests.windows.e2e.common import ADMIN_PASSWORD, recordings_root, write_inf, write_secrets
from tests.windows.conftest import E2E

pytestmark = [pytest.mark.e2e]

EXPECTED_TITLES = ("Condiciones", "Tipo de puesto", "Opciones", "Grabaciones", "Sede", "Seguridad", "Red",
                   "Todo listo", "Comprobación final")


@pytest.mark.paso("asistente", "Asistente completo (puesto de control) con captura de cada página")
def test_wizard_pages_are_captured(e2e: E2E, step: h.Step) -> None:
    step.details["limpieza"] = h.force_clean(e2e.env)
    inf = write_inf(e2e.work / "asistente.inf", {
        "SetupType": "control", "Tasks": "walls", "RecordingsDir": str(recordings_root() / "Grabaciones muros"),
        "Cameras": "16", "MbpsPerCamera": "4", "SiteName": "Puesto de control e2e", "SiteCode": "PC-01",
    })
    secrets = write_secrets(e2e.work / "asistente-secrets.json", admin_password=ADMIN_PASSWORD)
    out = e2e.artifacts / "capturas-asistente"
    log = e2e.env.log_path("asistente")
    res = wizard_capture.run(e2e.installer, out, [f"/LOADINF={inf}", f"/SECRETS={secrets}", "/MINFREEGB=1",
                                                  f"/LOG={log}"], env=e2e.env.environ())
    titles = [str(p["title"]) for p in res.pages]
    step.details["paginas"] = titles
    step.details["capturas"] = str(out)
    assert not res.error, res.error + "\n" + (h.read_text_any(log)[-4000:] if log.is_file() else "")
    assert res.exit_code == 0, h.read_text_any(log)[-4000:]
    for expected in EXPECTED_TITLES:
        assert any(expected.lower() in t.lower() for t in titles), f"falta la página «{expected}»: {titles}"
    pngs = sorted(out.glob("*.png"))
    assert len(pngs) >= len(EXPECTED_TITLES) + 2, pngs   # + bienvenida y fin
    for png in pngs:
        data = png.read_bytes()
        assert data.startswith(b"\x89PNG") and len(data) > 5000, f"captura vacía: {png.name}"
    step.note(f"{len(pngs)} capturas en el artefacto de CI «instalador-capturas»")

    # Muros al iniciar sesión (tarea «walls» del puesto de control).
    run = h.reg_get(h.RUN_KEY, "VMSMultimarcaMuros") or ""
    assert run.lower().endswith('\\bin\\vmshost.exe" viewer --walls'), run
    # «Abrir VMS Multimarca» de la página final abre el visor (el de prueba, en CI con dobles).
    if e2e.doubles:
        assert h.wait_until(lambda: bool(h.find_windows(h.VIEWER_DOUBLE_CLASS)), 30), "no se abrió el visor"
        for hwnd in h.find_windows(h.VIEWER_DOUBLE_CLASS):
            h.close_window(hwnd)
    (out / "resumen.json").write_text(json.dumps({"paginas": titles}, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    res_un = h.run_uninstall(e2e.env, ["/PURGE"], "asistente-desinstalar")
    assert res_un.code == 0
    assert h.reg_get(h.RUN_KEY, "VMSMultimarcaMuros") is None, "el desinstalador quita el arranque de los muros"
