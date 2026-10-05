"""Tubería de control (CONTRATO §15.2), public-status.json y proveedor del latido (§15.6, §7.3 bis)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vms_updater.control_pipe import UnixControlServer, handle_request, unix_request
from vms_updater.heartbeat import payload_update
from vms_updater.state_files import Blacklist

from .conftest import Site


def test_status_check_hold_rollback_lock(site: Site) -> None:
    eng = site.engine()
    r = handle_request(eng, {"cmd": "status"})
    assert r["ok"] and "journal" in r
    site.factory.release("2.1.0", comps=("app",))
    r = handle_request(eng, {"cmd": "hold", "on": True})
    assert r == {"ok": True}
    r = handle_request(eng, {"cmd": "check"})
    assert r["ok"] and r["found"] == "2.1.0" and r["result"] == "held"
    handle_request(eng, {"cmd": "hold", "on": False})
    r = handle_request(eng, {"cmd": "check"})
    assert r["ok"] and r["result"] == "update_ok"
    r = handle_request(eng, {"cmd": "rollback", "to": None, "reason": "pedido por soporte"})
    assert r["ok"] and r["update_id"].startswith("r-")
    assert site.pointer().active == "2.0.0"
    assert not Blacklist(site.layout.blacklist_file).contains("2.1.0")
    r = handle_request(eng, {"cmd": "rollback", "to": "9.9.9"})
    assert not r["ok"] and "no está instalada" in r["message_es"]
    # cerrojo compartido con el instalador
    assert handle_request(eng, {"cmd": "lock", "owner": "installer", "ttl_s": 60}) == {"ok": True}
    assert handle_request(eng, {"cmd": "lock", "owner": "otro", "ttl_s": 60}) == {"ok": False, "error": "busy"}
    assert handle_request(eng, {"cmd": "unlock", "owner": "otro"}) == {"ok": False, "error": "busy"}
    assert handle_request(eng, {"cmd": "unlock", "owner": "installer"}) == {"ok": True}
    assert handle_request(eng, {"cmd": "nada"})["error"] == "unknown_command"
    assert handle_request(eng, "texto")["error"] == "bad_request"
    assert handle_request(eng, {"cmd": "hold", "on": "sí"})["error"] == "bad_request"


def test_manual_rollback_warns_and_restores_config_from_backup(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    before = site.config_bytes()
    eng = site.engine()
    assert eng.check().result == "update_ok"
    assert site.config_bytes() != before                         # la migración de la 2.1.0 la cambió
    out = eng.manual_rollback(None, "prueba")
    assert out.result == "rollback_ok"
    assert site.config_bytes() == before
    assert site.running()["VMSBackend"] == "2.0.0"


@pytest.mark.skipif(sys.platform == "win32", reason="en Windows es la tubería con nombre (job B4 de CI)")
def test_unix_socket_server_round_trip_and_permissions(site: Site) -> None:
    import tempfile
    eng = site.engine()
    # macOS limita la ruta de un socket Unix a ~104 caracteres: la carpeta temporal de pytest es más larga
    sock = Path(tempfile.mkdtemp(prefix="vmsu-", dir="/tmp")) / "control.sock"
    srv = UnixControlServer(sock, lambda req: handle_request(eng, req))
    srv.start()
    try:
        assert (sock.stat().st_mode & 0o777) == 0o600
        r = unix_request(sock, {"cmd": "status"}, timeout=10)
        assert r["ok"] and r["installed"] in (None, "2.0.0")
    finally:
        srv.stop()
    assert not sock.exists()


def test_heartbeat_provider_reads_public_status(site: Site) -> None:
    assert payload_update(site.layout.data) is None
    site.engine().check()
    p = payload_update(site.layout.data)
    assert p is not None and p["installed"] == "2.0.0" and p["last_result"] == "no_update"
    assert set(p) <= {"installed", "channel", "state", "hold", "last_check", "last_result", "message_es",
                      "available", "metadata_expires", "clock_skew_s", "reboot_pending", "updated",
                      "updater_version"}
    assert len(json.dumps(p)) < 8 * 1024


def test_heartbeat_provider_is_wired_in_extras(site: Site) -> None:
    from vms.core.heartbeat_extras import collect_extras
    from vms.core.paths import AppPaths

    site.engine().check()
    extras = collect_extras(AppPaths(site.layout.data))
    assert extras["update"]["installed"] == "2.0.0"


def test_public_status_never_contains_secrets(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    site.engine(token="covert.SECRETO-DE-PRUEBA").check()
    text = site.layout.public_status_file.read_text()
    assert "SECRETO" not in text and "tok-interno" not in text
