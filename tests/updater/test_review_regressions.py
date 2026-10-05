"""Regresiones de la ronda de corrección de B4 (un caso por hallazgo de la revisión cruzada).

Cada prueba reproduce el fallo tal como lo encontró el revisor y comprueba el comportamiento corregido.
"""
from __future__ import annotations

import datetime as _dt
import errno
import json
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from tools.release.mirror import make_mirror
from tools.release.publish import PublishError
from vms_updater._atomic import atomic_write_json
from vms_updater.authenticode import SignatureInfo
from vms_updater.backup import create_backup, restore_config
from vms_updater.control_pipe import handle_request
from vms_updater.pointer import rebuild_pointer
from vms_updater.stage import StageError, verify_staged
from vms_updater.state_files import Blacklist, VersionMarks
from vms_updater.system import FakeSystem

from .conftest import OUT_OF_WINDOW, ReleaseFactory, Site, install

POLICY = {"subject_o": "Unmanned Studio SL", "subject_c": "ES", "issuers": ["Certum Code Signing 2021 CA"]}
UPDATER_FILES = {"vmsctl.exe": b"MZ vmsctl-slot", "vms_updater/__init__.py": b"__version__ = 'x'\n",
                 "runtime/python.txt": b"py"}


def _status(site: Site) -> dict[str, Any]:
    return dict(json.loads(site.layout.public_status_file.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- rollback manual (high)
def test_manual_rollback_is_not_undone_by_next_check(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    eng = site.engine()
    assert eng.check().result == "update_ok"
    out = eng.manual_rollback(None, "soporte")
    assert out.result == "rollback_ok" and "Permitir de nuevo" in out.message_es
    assert site.pointer().active == "2.0.0"
    out = eng.check()                                        # siguiente ciclo, dentro de la ventana
    assert out.result == "no_update" and out.available == "2.1.0"
    assert site.pointer().active == "2.0.0", "la 2.1.0 se volvió a instalar sola"
    st = _status(site)
    assert st["skipped"] == ["2.1.0"] and st["last_result"] == "no_update"
    # una versión mayor sí se instala (y la omitida sigue omitida, sin estorbar)
    site.factory.release("2.1.1", comps=("app",))
    assert eng.check().result == "update_ok" and site.pointer().active == "2.1.1"


def test_panel_rollback_then_unskip_from_panel(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    eng = site.engine()
    assert eng.check().result == "update_ok"
    atomic_write_json(site.layout.directive_file, {"rollback_to": "previous", "received": "2026-11-20T10:00:00Z"})
    out = eng.handle_directive_rollback()
    assert out is not None and out.result == "rollback_ok"
    assert eng.check().result == "no_update" and site.pointer().active == "2.0.0"
    assert eng.handle_directive_rollback() is None           # la misma petición no se repite
    # «Permitir de nuevo» del panel: se aplica una sola vez
    atomic_write_json(site.layout.directive_file, {"rollback_to": "previous", "received": "2026-11-20T10:00:00Z",
                                                   "unskip_at": "2026-11-20T11:00:00Z"})
    assert eng.handle_directive_unskip() == ["2.1.0"]
    assert eng.handle_directive_unskip() is None
    assert eng.handle_directive_rollback() is None           # el marcador de la vuelta atrás se conserva
    assert eng.check().result == "update_ok" and site.pointer().active == "2.1.0"


def test_failed_version_is_never_downgraded_to_manual_skip(tmp_path: Path) -> None:
    bl = Blacklist(tmp_path / "blacklist.json")
    bl.add("2.1.0", "health check")
    bl.add("2.1.0", "vuelta atrás manual", kind="manual")
    assert bl.kind_of("2.1.0") == "failed" and bl.skipped() == []
    assert bl.unskip() == [] and bl.contains("2.1.0")         # unskip no levanta una versión que falló


# --------------------------------------------------------------------------- Windows 10 con ESU (high)
def test_windows10_esu_gets_updates_with_default_descriptor(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), security=True, severity="critical")
    site.system.build = 19045          # Windows 10 22H2 (D11 lo admite con ESU)
    out = site.engine().check()
    assert out.result == "update_ok", out.message_es


def test_windows_build_min_still_blocks_older_builds(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), windows_build_min=22631)
    site.system.build = 19045
    out = site.engine().check()
    assert out.result == "error" and "22631" in out.message_es and site.pointer().active == "2.0.0"


# --------------------------------------------------------------------------- reloj muy adelantado (medium)
def test_clock_ahead_days_reports_clock_skew(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    site.factory.release("2.1.0", comps=("app",))
    import tuf.ngclient._internal.trusted_metadata_set as tms

    real = _dt.datetime
    ahead = real.now(_dt.timezone.utc) + _dt.timedelta(days=10)

    class Fake(real):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:  # noqa: ANN401
            return ahead

    class FakeMod:
        datetime = Fake
        timezone = _dt.timezone
        timedelta = _dt.timedelta

    monkeypatch.setattr(tms, "datetime", FakeMod)
    out = site.engine(now_utc=lambda: ahead).check()
    st = _status(site)
    assert out.result == "clock_skew", out.message_es
    assert st["clock_skew_s"] is not None and st["clock_skew_s"] > 9 * 86400
    assert "días" in out.message_es and site.pointer().active == "2.0.0"


# --------------------------------------------------------------------------- espejo USB (medium)
def test_mirror_for_version_behind_channel_is_refused(tmp_path: Path, factory: ReleaseFactory) -> None:
    factory.release("2.1.0", comps=("app",))
    factory.release("2.2.0", comps=("app",))           # el canal stable ya apunta a la 2.2.0
    with pytest.raises(PublishError, match="Ningún canal apunta a la 2.1.0"):
        make_mirror(factory.repos, "2.1.0", tmp_path / "usb")
    with pytest.raises(PublishError, match="está en la 2.2.0"):
        make_mirror(factory.repos, "2.1.0", tmp_path / "usb", channel="stable")


def test_mirror_by_channel_installs_channel_version(tmp_path: Path, factory: ReleaseFactory, repo_url: str) -> None:
    factory.release("2.1.0", comps=("app",))
    factory.release("2.2.0", comps=("app",), channel="pilot")
    usb = tmp_path / "usb"
    res = make_mirror(factory.repos, None, usb, channel="stable")
    assert res["version"] == "2.1.0" and res["channels"] == ["stable"] and res["channels_not_served"] == ["pilot"]
    site = install(tmp_path / "tienda", factory, repo_url, mode="offline")
    out = site.engine(source_url=usb.as_uri()).check()
    assert out.result == "update_ok", out.message_es
    assert site.pointer().active == "2.1.0"
    # una tienda en pilot con este USB: mensaje claro, no un «HTTP 404» a secas
    atomic_write_json(site.layout.local_config_file, {"channel": "pilot", "services": ["VMSBackend"]})
    out = site.engine(source_url=usb.as_uri()).check()
    assert out.result == "error" and "prepara un USB" in out.message_es and "404" not in out.message_es


def test_mirror_cli_defaults_to_stable_channel(tmp_path: Path, factory: ReleaseFactory) -> None:
    from tools.release.__main__ import build_parser
    a = build_parser().parse_args(["mirror", "--out", str(tmp_path / "usb")])
    assert a.channel is None and a.version is None                 # make_mirror elige stable
    res = make_mirror(factory.repos, a.version, a.out, channel=a.channel)
    assert res["version"] == "2.0.0" and res["channels"] == ["stable"]


# --------------------------------------------------------------------------- durabilidad (medium)
def test_zeroed_file_in_staged_version_is_detected_and_restaged(site: Site) -> None:
    """Corte de luz tras montar: un archivo con su tamaño pero lleno de ceros. No llega a ejecutarse."""
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine(now_local=OUT_OF_WINDOW).check().result == "waiting_window"
    vdir = site.layout.version_dir("2.1.0")
    victim = vdir / "app" / "VERSION.txt"
    size = victim.stat().st_size
    os.chmod(victim, stat.S_IRUSR | stat.S_IWUSR)
    victim.write_bytes(b"\0" * size)
    with pytest.raises(StageError, match="no coincide"):
        verify_staged(vdir)
    out = site.engine().check()                      # en la ventana: se vuelve a montar y se aplica
    assert out.result == "update_ok", out.message_es
    assert (vdir / "app" / "VERSION.txt").read_bytes() == b"2.1.0\n"
    verify_staged(vdir)


def test_damage_between_stage_and_apply_aborts_before_commit(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """El paso `downloaded` del diario vuelve a comprobar los manifiestos: si la carpeta se dañó, revierte sin
    tocar la versión activa, la borra y en el ciclo siguiente se monta de nuevo."""
    import vms_updater.engine as engmod

    site.factory.release("2.1.0", comps=("app",))
    real_stage = engmod.stage_version

    def stage_then_damage(**kw: Any) -> Path:
        vdir = real_stage(**kw)
        f = vdir / "app" / "VERSION.txt"
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)
        f.write_bytes(b"\0" * f.stat().st_size)
        return vdir

    monkeypatch.setattr(engmod, "stage_version", stage_then_damage)
    out = site.engine().check()
    assert out.result == "update_failed" and "dañada" in out.message_es
    assert site.pointer().active == "2.0.0" and not site.layout.version_dir("2.1.0").exists()
    assert not Blacklist(site.layout.blacklist_file).contains("2.1.0")      # se reintenta
    monkeypatch.setattr(engmod, "stage_version", real_stage)
    assert site.engine().check().result == "update_ok" and site.pointer().active == "2.1.0"


def test_staging_and_backup_fsync_every_file(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = os.fsync

    def counting(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine(now_local=OUT_OF_WINDOW).check().result == "waiting_window"
    vdir = site.layout.version_dir("2.1.0")
    staged_files = [p for p in vdir.rglob("*") if p.is_file()]
    after_stage = len(calls)
    assert after_stage >= len(staged_files)
    b = create_backup(data_dir=site.layout.data, backups_dir=site.layout.backups_dir, name="pre-x")
    backed = [p for p in b.rglob("*") if p.is_file()]
    assert len(calls) - after_stage >= len(backed)


def test_restore_skips_damaged_json_from_backup(site: Site) -> None:
    L = site.layout
    b = create_backup(data_dir=L.data, backups_dir=L.backups_dir, name="pre-2.1.0-x")
    (b / "config" / "config.json").write_bytes(b"\0" * 64)              # corte de luz: ceros
    good_now = b'{"version": 2, "nuevo": true}'
    atomic_write_json(L.config_dir / "config.json", json.loads(good_now))
    restored = restore_config(data_dir=L.data, backup_dir=b)
    assert "config/config.json" not in restored and "config/users.json" in restored
    assert json.loads((L.config_dir / "config.json").read_text()) == {"version": 2, "nuevo": True}
    assert not (L.config_dir / "config.json.after-rollback").exists()   # no se aparta el bueno


# --------------------------------------------------------------------------- puntero sin diario (medium)
def test_rebuild_pointer_without_journal_ignores_unapplied_staged(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine(now_local=OUT_OF_WINDOW).check().result == "waiting_window"
    assert VersionMarks(site.layout.versions_state_file).pending() == {"2.1.0"}
    site.layout.pointer_file.write_text("{corrupto")
    site.layout.journal_file.write_text("{corrupto")
    site.engine(now_local=OUT_OF_WINDOW).startup()
    ptr = site.pointer()
    assert ptr.active == "2.0.0" and not ptr.trial
    assert site.migrations == []


def test_rebuild_pointer_prefers_known_good_and_trials_the_unknown(tmp_path: Path, site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    marks = VersionMarks(site.layout.versions_state_file)
    assert marks.good()[0] == "2.1.0" and marks.pending() == set()
    vdir = site.layout.versions_dir
    p = rebuild_pointer(None, vdir, known_good=["2.0.0"], unverified=set())
    assert p is not None and p.active == "2.0.0" and not p.trial
    # sin ninguna información (registro de versiones dañado): la más alta, pero a prueba y con anterior
    p = rebuild_pointer(None, vdir, unverified=None)
    assert p is not None and p.active == "2.1.0" and p.trial and p.previous == "2.0.0"
    # el registro dañado no impide usar el diario
    site.layout.versions_state_file.write_text("{roto")
    assert VersionMarks(site.layout.versions_state_file).pending() is None
    site.layout.pointer_file.unlink()
    site.engine().startup()
    assert site.pointer().active == "2.1.0" and not site.pointer().trial         # last_good del diario


# --------------------------------------------------------------------------- actualizador A/B (medium)
def test_new_updater_not_confirmed_until_a_good_check(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    site.factory.release("2.1.0", comps=("app",), updater_files={**UPDATER_FILES, "VERSION": b"2.1.0"})
    assert site.engine(slot="a").check().result == "restart_updater"
    eng_b = site.engine(slot="b")
    assert eng_b.startup() is None and site.pointer().updater.trial
    # su comprobación falla (p. ej. un fallo del código nuevo): sigue a prueba y vmshost podrá volver a la A
    from vms_updater.client import TufClient

    def broken(self: Any) -> Any:
        raise RuntimeError("fallo del actualizador nuevo")

    monkeypatch.setattr(TufClient, "refresh", broken)
    assert eng_b.check().result == "error"
    assert site.pointer().updater.trial is True and eng_b.updater_trial_pending()
    j = site.journal()
    assert j is not None and j.kind == "updater" and j.in_progress
    monkeypatch.undo()
    assert eng_b.check().result == "no_update"
    assert site.pointer().updater.trial is False


def test_updater_slot_never_confirmed_when_slot_is_unknown(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), updater_files={**UPDATER_FILES, "VERSION": b"2.1.0"})
    assert site.engine(slot="a").check().result == "restart_updater"
    eng = site.engine(slot=None)
    assert eng.startup() is None
    eng.check()
    assert site.pointer().updater.trial is True


def test_updater_slot_goes_through_authenticode(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), authenticode=dict(POLICY),
                         updater_files={**UPDATER_FILES, "VERSION": b"2.1.0"})
    seen: list[str] = []

    def verifier(p: Path) -> SignatureInfo | None:
        seen.append(p.as_posix())
        bad = "slot-" in p.as_posix() and p.name == "vmsctl.exe"
        return SignatureInfo(not bad, "Unmanned Studio SL", "ES", "Certum Code Signing 2021 CA", "aa" * 32, True)

    out = site.engine(slot="a", verifier=verifier).check()
    assert out.result == "update_failed" and "actualizador" in out.message_es
    assert any("slot-b" in s for s in seen)
    ptr = site.pointer()
    assert ptr.active == "2.1.0" and ptr.updater.slot == "a" and not ptr.updater.trial   # la versión sí quedó
    assert not (site.layout.slot_dir("b") / "vmsctl.exe").exists()
    assert any(v.startswith("updater-2.1.0-") for v in Blacklist(site.layout.blacklist_file).versions())


# --------------------------------------------------------------------------- disco lleno (low)
def test_enospc_during_download_is_disk_full(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    from tuf.ngclient import Updater

    site.factory.release("2.1.0", comps=("app",))
    real = Updater.download_target

    def boom(self: Any, info: Any, filepath: Any = None, target_base_url: Any = None) -> str:
        if info.path.startswith("components/"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return str(real(self, info, filepath, target_base_url))

    monkeypatch.setattr(Updater, "download_target", boom)
    out = site.engine().check()
    assert out.result == "disk_full", out.message_es
    assert _status(site)["last_result"] == "disk_full"


def test_free_space_checked_in_temp_and_cache(site: Site) -> None:
    class SmallTemp(FakeSystem):
        def free_bytes(self, path: Path) -> int:
            return 10 if Path(path).resolve() == Path(tempfile.gettempdir()).resolve() else 10 ** 12

    site.system = SmallTemp()
    site.factory.release("2.1.0", comps=("app",))
    out = site.engine().check()
    assert out.result == "disk_full" and "temporal" in out.message_es


# --------------------------------------------------------------------------- cerrojo con el instalador (low)
def test_installer_lock_granted_during_download_but_not_while_applying(site: Site) -> None:
    eng = site.engine()
    started, release = threading.Event(), threading.Event()

    def long_download() -> None:          # comprobación/descarga: solo `_op`
        with eng._op:
            started.set()
            release.wait(10)

    th = threading.Thread(target=long_download)
    th.start()
    started.wait(5)
    try:
        assert handle_request(eng, {"cmd": "lock", "owner": "installer", "ttl_s": 60}) == {"ok": True}
    finally:
        release.set()
        th.join()
    # y la comprobación, al ir a aplicar, respeta el cerrojo
    site.factory.release("2.1.0", comps=("app",))
    out = eng.check()
    assert out.result == "waiting_window" and "installer" in out.message_es
    assert site.pointer().active == "2.0.0"


# --------------------------------------------------------------------------- claves (low)
@pytest.mark.skipif(sys.platform == "win32", reason="permisos POSIX (en Windows protege la carpeta del perfil)")
def test_private_files_are_born_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.release import keys as keymod
    from tools.release.site_token import write_secrets

    modes: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, *a: Any, **kw: Any) -> int:
        if flags & os.O_CREAT:
            modes.append(mode)
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    old = os.umask(0o022)
    try:
        keymod.write_private_file(tmp_path / "k.pem", b"PRIVADA")
        write_secrets(tmp_path / "secrets.json", "tok")
    finally:
        os.umask(old)
    assert modes and all(m == 0o600 for m in modes)
    for f in ("k.pem", "secrets.json"):
        assert stat.S_IMODE((tmp_path / f).stat().st_mode) == 0o600
    assert json.loads((tmp_path / "secrets.json").read_text())["update_token"] == "tok"
    assert not list(tmp_path.glob(".*.tmp-*"))

