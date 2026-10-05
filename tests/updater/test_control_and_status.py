"""Tubería de control (CONTRATO §15.2), public-status.json y proveedor del latido (§15.6, §7.3 bis)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vms_updater.control_pipe import UnixControlServer, handle_request
from vms_updater.pipe_client import unix_request
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
    bl = Blacklist(site.layout.blacklist_file)
    assert bl.kind_of("2.1.0") == "manual" and bl.skipped() == ["2.1.0"]      # omitida, no «fallida»
    r = handle_request(eng, {"cmd": "check"})
    assert r["ok"] and r["result"] == "no_update" and "Permitir de nuevo" in r["message_es"]
    assert site.pointer().active == "2.0.0"                                   # no se reinstala sola
    r = handle_request(eng, {"cmd": "unskip", "version": None})
    assert r == {"ok": True, "unskipped": ["2.1.0"], "skipped": []}
    assert handle_request(eng, {"cmd": "unskip", "version": 3})["error"] == "bad_request"
    r = handle_request(eng, {"cmd": "check"})
    assert r["ok"] and r["result"] == "update_ok" and site.pointer().active == "2.1.0"
    r = handle_request(eng, {"cmd": "rollback", "to": None, "reason": "otra vez"})
    assert r["ok"] and site.pointer().active == "2.0.0"
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


def test_lock_is_busy_during_an_update_and_status_stays_responsive(site: Site) -> None:
    import tempfile
    import threading
    import time

    eng = site.engine()
    started, release = threading.Event(), threading.Event()

    def long_update() -> None:            # simula una actualización APLICÁNDOSE (cerrojo `_apply`)
        with eng._op, eng._apply:
            started.set()
            release.wait(10)

    th = threading.Thread(target=long_update)
    th.start()
    started.wait(5)
    try:
        assert handle_request(eng, {"cmd": "lock", "owner": "installer", "ttl_s": 60})["error"] == "busy"
        if sys.platform != "win32":
            sock = Path(tempfile.mkdtemp(prefix="vmsu-", dir="/tmp")) / "c.sock"
            srv = UnixControlServer(sock, lambda req, caller: handle_request(eng, req, caller))
            srv.start()
            try:
                t0 = time.monotonic()
                assert unix_request(sock, {"cmd": "status"}, timeout=5)["ok"]
                assert time.monotonic() - t0 < 2
            finally:
                srv.stop()
    finally:
        release.set()
        th.join()
    assert handle_request(eng, {"cmd": "lock", "owner": "installer", "ttl_s": 60}) == {"ok": True}


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
    srv = UnixControlServer(sock, lambda req, caller: handle_request(eng, req, caller))
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
    assert p["window"] == "01:00-03:00" and p["skipped"] == []
    assert set(p) <= {"installed", "channel", "state", "hold", "window", "skipped", "last_check", "last_result",
                      "message_es",
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


@pytest.mark.skipif(sys.platform != "win32", reason="tubería con nombre de Windows (job B4 de CI)")
def test_windows_named_pipe_round_trip_and_acl(site: Site) -> None:  # pragma: no cover - Windows
    import time
    import uuid

    from vms_updater.control_pipe import WindowsPipeServer
    from vms_updater.pipe_client import ServerNotTrusted, current_user_sid, pipe_request

    me = frozenset({current_user_sid()})        # el servidor de la prueba no es SYSTEM: se confía en el runner
    eng = site.engine()
    name = r"\\.\pipe\VMSMultimarca.updater.prueba-" + uuid.uuid4().hex[:8]
    srv = WindowsPipeServer(lambda req, caller: handle_request(eng, req, caller), name=name,
                            restricted_sids=frozenset())
    srv.start()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                r = pipe_request({"cmd": "status"}, name=name, trusted_sids=me)
                break
            except FileNotFoundError:
                assert time.monotonic() < deadline, "la tubería no apareció"
                time.sleep(0.05)
        assert r["ok"] and "journal" in r
        assert pipe_request({"cmd": "lock", "owner": "installer", "ttl_s": 30}, name=name, trusted_sids=me) == \
            {"ok": True}
        # sin confiar en la cuenta del servidor (no es SYSTEM), el cliente no envía nada (suplantación)
        with pytest.raises(ServerNotTrusted):
            pipe_request({"cmd": "status"}, name=name, trusted_sids=frozenset({"S-1-5-18"}))
    finally:
        srv.stop()
    # un cliente identificado como cuenta de latido solo puede entregar la directiva
    hb = r"\\.\pipe\VMSMultimarca.updater.latido-" + uuid.uuid4().hex[:8]
    srv3 = WindowsPipeServer(lambda req, caller: handle_request(eng, req, caller), name=hb, restricted_sids=me)
    srv3.start()
    try:
        time.sleep(0.5)
        assert pipe_request({"cmd": "rollback"}, name=hb, trusted_sids=me)["error"] == "forbidden"
        assert pipe_request({"cmd": "directive", "directive": {"check": True}}, name=hb, trusted_sids=me)["ok"]
    finally:
        srv3.stop()
    # con una ACL que solo deja entrar a SYSTEM, el administrador de la prueba no puede abrirla
    closed = r"\\.\pipe\VMSMultimarca.updater.solo-system-" + uuid.uuid4().hex[:8]
    srv2 = WindowsPipeServer(lambda req, caller: {"ok": True}, name=closed, sddl="D:P(A;;GA;;;SY)")
    srv2.start()
    try:
        time.sleep(0.5)
        with pytest.raises(PermissionError):
            pipe_request({"cmd": "status"}, name=closed, trusted_sids=me)
    finally:
        srv2._stop.set()
