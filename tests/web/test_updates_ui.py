"""/status muestra el estado de la actualización de la sede (B4, CONTRATO §15.6 y §18.9)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.web.conftest import OPERATOR, PageErrors, login

ALLOWED = ("401 (Unauthorized)",)


def _write_status(backend: Any, **fields: Any) -> None:
    f = Path(backend.paths.base) / "updater" / "public-status.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"schema": 1, **fields}), encoding="utf-8")


def test_status_page_shows_update_state(fake_stub: Any, make_context: Any, screenshots_dir: Path) -> None:
    backend, server = fake_stub
    page = make_context(server.base_url).new_page()
    errors = PageErrors().attach(page)
    login(page, next_path="/status")
    page.wait_for_selector("#updates-root:not([hidden]) #h-st-updates")
    assert "Sin comprobar todavía" in page.inner_text("#updates-root")
    _write_status(backend, installed="2.0.0", channel="pilot", state="rolled_back", last_result="update_failed",
                  message_es="La 2.1.0 falló (el motor de vídeo no está en marcha): se volvió a la 2.0.0",
                  last_check="2026-11-20T01:10:00Z", available=None, metadata_expires="2026-11-27T00:00:00Z",
                  clock_skew_s=0.4, hold=True, reboot_pending=True, window="02:00-04:00", skipped=["2.1.0"])
    page.evaluate("() => import('/static/js/updates.js').then((m) => m.refresh())")
    page.wait_for_selector("#updates-root .upd-msg.bad")
    text = page.inner_text("#updates-root")
    assert "se volvió a la 2.0.0" in text and "pilot" in text and "Retenida" in text
    assert "Reinicio pendiente" in text and "+0.4 s" in text
    assert "02:00-04:00" in text and "Omitida tras volver atrás" in text
    assert page.locator("#updates-root .pill.bad").count() == 1
    page.screenshot(path=str(screenshots_dir / "estado-actualizaciones.png"), full_page=True)
    assert errors.unexpected(ALLOWED) == []


def test_operator_sees_update_state_and_no_actions(fake_stub: Any, make_context: Any) -> None:
    backend, server = fake_stub
    _write_status(backend, installed="2.1.0", channel="stable", state="good", last_result="update_ok",
                  message_es="Actualizado a la 2.1.0")
    page = make_context(server.base_url).new_page()
    login(page, user=OPERATOR, next_path="/status")
    page.wait_for_selector("#updates-root:not([hidden]) .pill.ok")
    # aquí no se actualiza ni se revierte (el «?» de ayuda de B6 no es una acción)
    assert page.locator("#updates-root button:not(.help-btn)").count() == 0
