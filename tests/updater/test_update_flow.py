"""Casos de PLAN-V2 §4.4: actualización normal, ataques que TUF rechaza, reloj, caducidad, health check y
vuelta atrás, disco lleno, reinicio pendiente, ventana, espejo USB y lista negra."""
from __future__ import annotations

import errno
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from securesystemslib.signer import CryptoSigner

from tools.release.mirror import make_mirror
from tools.release.publish import set_channel
from vms_updater import stage as stage_mod
from vms_updater.models import bundle_target
from vms_updater.state_files import Blacklist, StatusFile
from vms_updater.system import installer_may_install

from .conftest import IN_WINDOW, OUT_OF_WINDOW, SERVICES, ReleaseFactory, Site, install, serve_dir


def _status(site: Site) -> dict:
    return StatusFile(site.layout.public_status_file).read().dump()


# --------------------------------------------------------------------------- camino feliz
def test_app_update_downloads_only_app_backs_up_switches_and_marks_good(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",))
    eng = site.engine()
    assert eng.startup() is None
    out = eng.check()
    assert out.result == "update_ok", out.message_es
    ptr = site.pointer()
    assert ptr.active == "2.1.0" and ptr.previous == "2.0.0" and ptr.trial is False
    j = site.journal()
    assert j is not None and j.state == "good" and j.last_good == "2.1.0" and j.components == ["app"]
    assert [s.state for s in j.steps] == ["downloaded", "backed_up", "stopping", "switched", "migrated", "started",
                                          "verifying", "good"]
    assert all(s.done_unix is not None for s in j.steps)
    # solo se descargó el componente que cambió
    cached = sorted(p.name for p in site.layout.tuf_targets_dir.rglob("*.zip"))
    assert cached == ["app-2.1.0.zip"]
    # el resto se enlazó desde la 2.0.0 (mismo inodo: enlace duro)
    old = site.layout.version_dir("2.0.0") / "runtime" / "python.txt"
    new = site.layout.version_dir("2.1.0") / "runtime" / "python.txt"
    assert new.read_bytes() == old.read_bytes() and new.stat().st_ino == old.stat().st_ino
    # motor intacto: la grabación no se corta al actualizar app
    assert "VMSEngine" not in j.services
    assert site.running() == {"VMSEngine": "2.0.0", "VMSBackend": "2.1.0", "VMSHeartbeat": "2.1.0"}
    assert site.migrations == ["2.1.0"]
    assert j.backup and (site.layout.data / j.backup / "config" / "config.json").is_file()
    assert site.system.registry == {"InstalledVersion": "2.1.0", "DisplayVersion": "2.1.0"}
    st = _status(site)
    assert st["last_result"] == "update_ok" and st["installed"] == "2.1.0"
    # una segunda comprobación no hace nada
    assert eng.check().result == "no_update"


def test_engine_update_restarts_engine_only_in_window(site: Site) -> None:
    site.factory.release("2.1.0", comps=("engine",))
    out = site.engine(now_local=OUT_OF_WINDOW).check()
    assert out.result == "waiting_window"
    assert (site.layout.version_dir("2.1.0") / "release.json").is_file()      # ya montada, sin cortar nada
    assert site.pointer().active == "2.0.0"
    out = site.engine(now_local=IN_WINDOW).check()
    assert out.result == "update_ok"
    assert site.journal().services == ["VMSEngine"]


def test_critical_security_update_outside_window_only_without_engine(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), security=True, severity="critical")
    assert site.engine(now_local=OUT_OF_WINDOW).check().result == "update_ok"
    site.factory.release("2.2.0", comps=("engine",), security=True, severity="critical")
    assert site.engine(now_local=OUT_OF_WINDOW).check().result == "waiting_window"


# --------------------------------------------------------------------------- ataques (TUF)
def test_target_with_altered_hash_is_rejected_and_nothing_changes(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",))
    desc, _ = f.descriptor("2.1.0")
    ref = desc.components["app"]
    p = f.repos.online.target_file_path(ref.target, ref.sha256)
    data = p.read_bytes()
    p.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    before = site.config_bytes()
    out = site.engine().check()
    assert out.result == "error" and "hash" in out.message_es
    assert site.pointer().active == "2.0.0"
    assert not site.layout.version_dir("2.1.0").exists()
    assert site.config_bytes() == before


def test_targets_signed_with_unauthorized_key_is_rejected(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",), channel=None)
    repo = f.repos.online
    from tools.release.publish import channel_doc
    repo.add_target("channels/stable.json", channel_doc("stable", "2.1.0", paused=False), {"kind": "channel"})
    repo.write_targets([CryptoSigner.generate_ecdsa()])        # atacante sin la llave física
    repo.write_snapshot_timestamp(f.kr)                         # ...pero con las claves de CI
    out = site.engine().check()
    assert out.result == "error" and "no son de confianza" in out.message_es
    assert site.pointer().active == "2.0.0"


def test_rollback_attack_with_older_timestamp_is_rejected(site: Site) -> None:
    f = site.factory
    eng = site.engine()
    assert eng.check().result == "no_update"                    # el equipo ya vio la versión N
    ts = f.repos.online.meta_dir / "timestamp.json"
    old = ts.read_bytes()
    f.release("2.1.0", comps=("app",))
    assert site.engine(now_local=OUT_OF_WINDOW).check().result == "waiting_window"
    ts.write_bytes(old)                                          # el servidor vuelve a servir uno anterior
    out = site.engine().check()
    assert out.result == "error" and "no son de confianza" in out.message_es


def test_expired_timestamp_means_metadata_expired_and_no_update(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",), meta="ci")                # snapshot/timestamp sin refrescar…
    f.repos.online.write_snapshot_timestamp(f.kr, days=-1)       # …y caducado (metadatos congelados)
    out = site.engine().check()
    assert out.result == "metadata_expired"
    assert site.pointer().active == "2.0.0"
    st = _status(site)
    assert st["last_result"] == "metadata_expired" and "caducado" in st["message_es"]


def test_clock_skew_never_applies_and_does_not_loop(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    skewed = site.engine(now_utc=lambda: datetime.now(timezone.utc) + timedelta(minutes=10))
    for _ in range(3):
        out = skewed.check()
        assert out.result == "clock_skew" and "desfasado" in out.message_es
    assert site.pointer().active == "2.0.0"
    assert _status(site)["clock_skew_s"] > 500
    j = site.journal()
    assert j is not None and j.state == "good" and j.to == "2.0.0"          # nada empezado
    assert site.engine().check().result == "update_ok"                      # con el reloj bien, sí


def test_root_rotation_two_of_three_is_followed(site: Site) -> None:
    f = site.factory
    eng = site.engine()
    assert eng.check().result == "no_update"
    kr = f.kr
    from tools.release.keys import add_software_key
    new = add_software_key(kr, "dev-root-4", ["root"])
    old3 = kr.keys.pop("dev-root-3")
    kr.save()
    signers = [kr.signer("dev-root-1"), kr.signer("dev-root-2"), kr.signer("dev-root-4")]
    for r in f.repos.each():
        r.rotate_root(signers=signers, revoke=[old3.keyid], add=[new.public])
    f.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    cached_root = json.loads((site.layout.tuf_metadata_dir / "online" / "root.json").read_text())
    assert cached_root["signed"]["version"] == 2
    assert old3.keyid not in cached_root["signed"]["roles"]["root"]["keyids"]


# --------------------------------------------------------------------------- reglas de versión
def test_min_from_greater_than_installed_is_not_applied(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), min_from="2.0.5")
    out = site.engine().check()
    assert out.result == "min_from" and "2.0.5" in out.message_es
    assert site.pointer().active == "2.0.0"


def test_channel_paused_and_held_site(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",))
    set_channel(f.repos, "stable", pause=True)
    assert site.engine().check().result == "no_update"
    set_channel(f.repos, "stable", pause=False)
    atomic = site.layout.local_config_file
    cfg = json.loads(atomic.read_text())
    cfg["hold"] = True
    atomic.write_text(json.dumps(cfg))
    out = site.engine().check()
    assert out.result == "held" and out.available == "2.1.0"
    assert site.pointer().active == "2.0.0"


def test_channel_never_downgrades(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    set_channel(f.repos, "stable", version="2.0.0")              # «revertir» la publicación
    assert site.engine().check().result == "no_update"
    assert site.pointer().active == "2.1.0"


def test_central_directive_changes_channel_and_hold(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",), channel="pilot")
    eng = site.engine()
    assert eng.check().result == "no_update"                     # stable sigue en 2.0.0
    (site.layout.directive_file).write_text(json.dumps({"channel": "pilot", "received": "t1"}))
    assert eng.check().result == "update_ok"
    assert json.loads(site.layout.local_config_file.read_text())["channel"] == "pilot"


# --------------------------------------------------------------------------- health check y vuelta atrás
def test_health_check_failure_rolls_back_blacklists_and_reports(site: Site) -> None:
    f = site.factory
    f.release("2.1.0", comps=("app",), broken=True)
    before = site.config_bytes()
    out = site.engine().check()
    assert out.result == "update_failed", out.message_es
    ptr = site.pointer()
    assert ptr.active == "2.0.0" and ptr.trial is False
    assert site.config_bytes() == before                          # idéntico al respaldo (hash)
    j = site.journal()
    assert j is not None and j.state == "rolled_back" and j.last_good == "2.0.0"
    assert Blacklist(site.layout.blacklist_file).contains("2.1.0")
    assert site.running()["VMSBackend"] == "2.0.0"
    assert site.system.registry["DisplayVersion"] == "2.0.0"
    st = _status(site)
    assert st["last_result"] == "update_failed" and "2.1.0" in st["message_es"]
    # una segunda comprobación no la reintenta…
    assert site.engine().check().result == "no_update"
    # …pero una versión mayor sí
    f.release("2.1.1", comps=("app",))
    assert site.engine().check().result == "update_ok"
    assert site.pointer().active == "2.1.1"


def test_fewer_cameras_recording_counts_as_failure(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), fewer_cameras=True)
    assert site.engine().check().result == "update_failed"
    assert site.pointer().active == "2.0.0"


def test_disk_full_mid_extraction_cleans_tmp_and_retries_next_cycle(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    site.factory.release("2.1.0", comps=("app",))
    calls = {"n": 0}
    real = stage_mod._copy_stream

    def full(src, dst, length=0):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(src, dst, length)

    monkeypatch.setattr(stage_mod, "_copy_stream", full)
    out = site.engine().check()
    assert out.result == "disk_full"
    assert not site.layout.staging_dir("2.1.0").exists() and not site.layout.version_dir("2.1.0").exists()
    assert site.pointer().active == "2.0.0"
    monkeypatch.setattr(stage_mod, "_copy_stream", real)
    assert site.engine().check().result == "update_ok"


def test_not_enough_free_space_is_checked_before_downloading(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    site.system.free = 10 * 1024 ** 2
    assert site.engine().check().result == "disk_full"
    assert not list(site.layout.tuf_targets_dir.rglob("*.zip"))


def test_pending_windows_reboot_waits_for_next_window(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    site.system.pending_reboot = True
    out = site.engine().check()
    assert out.result == "reboot_pending"
    assert site.pointer().active == "2.0.0" and _status(site)["reboot_pending"] is True
    site.system.pending_reboot = False
    assert site.engine().check().result == "update_ok"


def test_installer_lock_blocks_update(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    eng = site.engine()
    assert eng.lock.acquire("installer", 600)
    assert eng.check().result == "waiting_window"
    assert eng.lock.release("installer")
    assert eng.check().result == "update_ok"


def test_old_setup_refuses_to_downgrade_after_updater_wrote_registry(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    ok, msg = installer_may_install("2.0.0", site.system.installed_version())
    assert not ok and "2.1.0" in msg and "/ALLOWDOWNGRADE" in msg
    assert installer_may_install("2.0.0", site.system.installed_version(), allow_downgrade=True)[0]
    assert installer_may_install("2.2.0", site.system.installed_version())[0]


# --------------------------------------------------------------------------- espejo USB (repositorio offline)
def test_file_mirror_gives_same_result_as_http(tmp_path: Path, factory: ReleaseFactory, repo_url: str) -> None:
    factory.release("2.1.0", comps=("app",))
    usb = tmp_path / "usb"
    res = make_mirror(factory.repos, "2.1.0", usb)
    assert res["files"] >= 2
    site = install(tmp_path / "tienda", factory, repo_url, mode="offline")
    out = site.engine(source_url=usb.as_uri()).check()
    assert out.result == "update_ok", out.message_es
    assert site.pointer().active == "2.1.0"


def test_usb_older_than_60_days_is_not_applied(tmp_path: Path, factory: ReleaseFactory, repo_url: str) -> None:
    factory.release("2.1.0", comps=("app",))
    usb = tmp_path / "usb"
    make_mirror(factory.repos, "2.1.0", usb, now=datetime.now(timezone.utc) - timedelta(days=61))
    site = install(tmp_path / "tienda", factory, repo_url, mode="offline")
    out = site.engine(source_url=usb.as_uri()).check()
    assert out.result == "metadata_expired" and "prepara un USB nuevo" in out.message_es
    assert site.pointer().active == "2.0.0"


def test_online_root_is_not_trusted_for_offline_repo(tmp_path: Path, factory: ReleaseFactory, repo_url: str) -> None:
    """Un equipo en modo online no acepta el repositorio offline (otro root) y viceversa."""
    factory.release("2.1.0", comps=("app",))
    site = install(tmp_path / "t", factory, repo_url, mode="online")
    with serve_dir(factory.repo_dir / "offline") as off_url:
        out = site.engine(source_url=off_url).check()
    assert out.result == "error"


# --------------------------------------------------------------------------- componente data (avisos)
def test_advisories_data_component_installed_newest_only(site: Site, tmp_path: Path) -> None:
    from tools.release.publish import PublishError, publish_advisories

    f = site.factory
    t1 = tmp_path / "a1.json"
    t1.write_text(json.dumps({"schema": 1, "generated_at": "2026-10-05T00:00:00Z", "advisories": [
        {"id": "ADV-2026-001", "vendor": "hikvision"}]}))
    publish_advisories(f.repos, t1)
    eng = site.engine()
    eng.check()
    dest = site.layout.advisories_file
    assert json.loads(dest.read_text())["generated_at"] == "2026-10-05T00:00:00Z"
    t2 = tmp_path / "a2.json"
    t2.write_text(json.dumps({"schema": 1, "generated_at": "2026-11-01T00:00:00Z", "advisories": []}))
    publish_advisories(f.repos, t2)
    eng.check()
    assert json.loads(dest.read_text())["generated_at"] == "2026-11-01T00:00:00Z"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": 2, "advisories": []}))
    with pytest.raises(PublishError):
        publish_advisories(f.repos, bad)


def test_bundle_descriptor_sha_must_match_targets(site: Site) -> None:
    """Defensa en profundidad: el descriptor y targets.json firmados tienen que cuadrar."""
    f = site.factory
    f.release("2.1.0", comps=("app",))
    desc, raw = f.descriptor("2.1.0")
    doc = json.loads(raw)
    doc["components"]["app"]["sha256"] = hashlib.sha256(b"otro").hexdigest()
    forged = (json.dumps(doc, sort_keys=True, indent=2) + "\n").encode()
    f.repos.add_target(bundle_target("2.1.0"), forged, {"kind": "bundle", "version": "2.1.0"})
    f.repos.commit()
    out = site.engine().check()
    assert out.result in ("error", "update_failed")
    assert site.pointer().active == "2.0.0"


def test_services_installed_filter(site: Site) -> None:
    desc, _ = site.factory.descriptor("2.0.0")
    eng = site.engine()
    assert eng.affected_services(desc, ["app"]) == ["VMSBackend", "VMSHeartbeat"]
    assert set(SERVICES) >= set(eng.affected_services(desc, ["app", "engine", "viewer"]))
