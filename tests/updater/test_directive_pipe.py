"""Directivas del panel por la tubería de control (revisión v2, A4 y Seguridad ALTO 2).

La cuenta del latido (`NT SERVICE\\VMSHeartbeat` o `VMSBackend`) solo puede entregar la directiva; el
actualizador la valida y la escribe él (los servicios no escriben en `updater\\`).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from central.directive import deliver
from tests.updater.conftest import Site
from vms_updater.control_pipe import PIPE_SDDL, UnixControlServer, handle_request
from vms_updater.pipe_client import CLIENT_ACCESS, service_sid
from vms_updater.state_files import read_directive


def test_heartbeat_account_can_only_deliver_the_directive(site: Site) -> None:
    eng = site.engine()
    for cmd in ("rollback", "check", "unskip", "hold", "lock", "unlock"):
        r = handle_request(eng, {"cmd": cmd}, "heartbeat")
        assert r["error"] == "forbidden", cmd
    assert handle_request(eng, {"cmd": "status"}, "heartbeat")["ok"]
    r = handle_request(eng, {"cmd": "directive", "directive": {"check": True, "rollback_to": "previous"}},
                       "heartbeat")
    assert r == {"ok": True}
    d = read_directive(eng.layout.directive_file)
    assert d is not None and d.check and d.rollback_to == "previous"


def test_invalid_directives_are_rejected_and_none_clears(site: Site) -> None:
    eng = site.engine()
    for bad in ({"channel": "CANAL MALO"}, {"rollback_to": "../../etc"}, "texto", {"hold": "sí"}):
        r = handle_request(eng, {"cmd": "directive", "directive": bad}, "heartbeat")
        assert not r["ok"], bad
    assert handle_request(eng, {"cmd": "directive", "directive": {"hold": True}}, "heartbeat")["ok"]
    assert eng.layout.directive_file.is_file()
    assert handle_request(eng, {"cmd": "directive", "directive": None}, "heartbeat") == {"ok": True}
    assert not eng.layout.directive_file.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="socket Unix (la tubería de Windows va en el job B4)")
def test_deliver_reaches_the_updater_through_its_control_socket(site: Site) -> None:
    eng = site.engine()
    short = Path(tempfile.mkdtemp(prefix="vmsu-", dir="/tmp"))    # AF_UNIX: ruta de 108 bytes como mucho
    srv = UnixControlServer(short / "control.sock", lambda req, caller: handle_request(eng, req, caller))
    srv.start()
    try:
        assert deliver({"update": {"check": True, "channel": "pilot", "received": "2026-10-05T10:00:00Z"}}, short)
    finally:
        srv.stop()
        shutil.rmtree(short, ignore_errors=True)
    raw = json.loads(eng.layout.directive_file.read_text(encoding="utf-8"))
    assert raw["check"] is True and raw["channel"] == "pilot"


def test_deliver_never_raises_without_updater(tmp_path: Path) -> None:
    assert deliver({"update": {"check": True}}, tmp_path / "updater") is False


def test_pipe_acl_lets_heartbeat_accounts_in_without_create_instance() -> None:
    assert "(A;;GA;;;SY)(A;;GA;;;BA)" in PIPE_SDDL
    for svc in ("VMSHeartbeat", "VMSBackend"):
        assert f"(A;;0x{CLIENT_ACCESS:x};;;{service_sid(svc)})" in PIPE_SDDL
    assert not CLIENT_ACCESS & 0x0004, "FILE_CREATE_PIPE_INSTANCE nunca para un cliente"
    # mismo SID que calcula Windows para NT SERVICE\VMSHeartbeat (sc showsid)
    assert service_sid("VMSHeartbeat").startswith("S-1-5-80-") and service_sid("vmsheartbeat") == \
        service_sid("VMSHeartbeat")


# --------------------------------------------------------------------------- bajos de la revisión
def test_pointer_and_manual_rollback_reject_paths_as_versions(site: Site) -> None:
    from pydantic import ValidationError

    from vms_updater.models import ActivePointer

    for bad in ("", "..", "../../Windows", "2.0.0/../x", "a\\b", ".oculta"):
        with pytest.raises(ValidationError):
            ActivePointer(active=bad)
    assert ActivePointer(active="2.1.0-rc.1+build.5", previous="2.0.0").previous == "2.0.0"
    out = site.engine().manual_rollback("../../ProgramData")
    assert out.result == "error" and "no válida" in out.message_es


def test_missing_update_source_is_a_clear_error_not_unexpected(site: Site) -> None:
    from vms_updater.client import UpdateSourceError

    eng = site.engine()

    def no_source() -> object:
        raise UpdateSourceError("No hay fuente de actualizaciones configurada (VMS_UPDATE_SOURCE)")

    eng.d.client_factory = no_source   # type: ignore[assignment]
    out = eng.check()
    assert out.result == "error" and "VMS_UPDATE_SOURCE" in out.message_es
    assert "inesperado" not in eng.status.read().message_es.lower()


def test_health_check_follows_the_installed_http_port() -> None:
    """A3: con un puerto HTTP personalizado (instalador → .env → vmsctl run), el health check mira ese puerto."""
    from vms_updater.service import backend_url

    assert backend_url({}) == "http://127.0.0.1:8600"
    assert backend_url({"VMS_HTTP_PORT": "8601"}) == "http://127.0.0.1:8601"
    assert backend_url({"VMS_HTTP_PORT": "x"}) == "http://127.0.0.1:8600"
    assert backend_url({"VMS_BACKEND_URL": "http://127.0.0.1:9000/", "VMS_HTTP_PORT": "8601"}) == \
        "http://127.0.0.1:9000"
