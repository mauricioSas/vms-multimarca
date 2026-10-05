"""Actualizador A/B y puntero (PLAN-V2 §2.5 paso 10, CONTRATO §13.3-§13.4)."""
from __future__ import annotations

import json
import time

from vms_updater.engine import RELOAD_EXIT_CODE
from vms_updater.journal import JournalStore
from vms_updater.models import ActivePointer
from vms_updater.pointer import PointerStore, rebuild_pointer
from vms_updater.state_files import Blacklist

from .conftest import Site
from .doubles import vmshost_double

UPDATER_FILES = {"vmsctl.exe": b"MZ vmsctl-slot", "vms_updater/__init__.py": b"__version__ = 'x'\n",
                 "runtime/python.txt": b"py"}


def _publish_updater(site: Site, version: str = "2.1.0", files: dict[str, bytes] | None = None) -> None:
    site.factory.release(version, comps=("app",), updater_files=files or {**UPDATER_FILES,
                                                                         "VERSION": version.encode()})


def test_new_updater_goes_to_inactive_slot_and_confirms_on_start(site: Site) -> None:
    _publish_updater(site)
    out = site.engine(slot="a").check()
    assert out.result == "restart_updater" and RELOAD_EXIT_CODE == 3
    ptr = site.pointer()
    assert ptr.active == "2.1.0"                                   # la versión ya quedó buena antes
    assert ptr.updater.slot == "b" and ptr.updater.previous_slot == "a" and ptr.updater.trial
    assert (site.layout.slot_dir("b") / "vmsctl.exe").read_bytes() == b"MZ vmsctl-slot"
    j = site.journal()
    assert j is not None and j.kind == "updater" and j.state == "started"
    # vmshost relanza el servicio desde la ranura B y el actualizador nuevo confirma
    out2 = site.engine(slot="b").startup()
    assert out2 is not None and out2.result == "update_ok"
    ptr = site.pointer()
    assert ptr.updater.slot == "b" and ptr.updater.trial is False
    j = site.journal()
    assert j is not None and j.kind == "updater" and j.state == "good" and not j.in_progress
    # una segunda comprobación no reinstala el mismo actualizador
    assert site.engine(slot="b").check().result == "no_update"


def test_broken_new_updater_is_reverted_by_vmshost_and_blacklisted(site: Site) -> None:
    _publish_updater(site)
    assert site.engine(slot="a").check().result == "restart_updater"
    # la ranura B no arranca: vmshost la ve caer 3 veces y vuelve a la A (< 10 min)
    now = time.time()
    assert vmshost_double.watch_updater(site.layout, crashes=3, now=now + 60) == "vuelta a la ranura a"
    ptr = site.pointer()
    assert ptr.updater.slot == "a" and not ptr.updater.trial
    out = site.engine(slot="a").startup()
    assert out is not None and out.result == "update_failed" and "no arrancó" in out.message_es
    status = json.loads(site.layout.public_status_file.read_text())
    assert status["last_result"] == "update_failed"
    assert any(v.startswith("updater-2.1.0-") for v in Blacklist(site.layout.blacklist_file).versions())
    j = site.journal()
    assert j is not None and not j.in_progress
    # no se reintenta ese actualizador
    assert site.engine(slot="a").check().result == "no_update"
    assert site.pointer().updater.slot == "a"


def test_unconfirmed_updater_slot_reverted_after_30_min(site: Site) -> None:
    _publish_updater(site)
    site.engine(slot="a").check()
    since = site.pointer().updater.trial_since_unix
    assert since is not None
    assert vmshost_double.watch_updater(site.layout, crashes=0, now=since + 600) == "esperando"
    assert vmshost_double.watch_updater(site.layout, crashes=0, now=since + 1800) == "vuelta a la ranura a"


def test_active_json_missing_or_corrupt_is_rebuilt_from_journal(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    for damage in (lambda p: p.unlink(), lambda p: p.write_text("{corrupto"), lambda p: p.write_text("[]")):
        damage(site.layout.pointer_file)
        assert vmshost_double.ensure_pointer(site.layout, time.time()) == "reconstruido:2.1.0"
        damage(site.layout.pointer_file)
        site.engine().startup()                       # el actualizador también lo reconstruye
        assert site.pointer().active == "2.1.0"


def test_rebuild_without_journal_uses_highest_installed_version(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"
    site.layout.journal_file.unlink()
    site.layout.pointer_file.unlink()
    ptr = rebuild_pointer(None, site.layout.versions_dir)
    assert ptr is not None and ptr.active == "2.1.0"


def test_pointer_keeps_unknown_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "active.json"
    p.write_text(json.dumps({"schema": 1, "active": "2.0.0", "previous": None, "trial": False,
                             "updater": {"slot": "a"}, "updated_unix": 1, "x-b1": {"pid": 4}}))
    ps = PointerStore(p)
    ps.switch("2.1.0")
    doc = json.loads(p.read_text())
    assert doc["x-b1"] == {"pid": 4} and doc["active"] == "2.1.0" and doc["previous"] == "2.0.0" and doc["trial"]
    assert isinstance(doc["trial_since_unix"], int)


def test_trial_version_failing_is_reverted_by_vmshost_and_updater_finishes(site: Site) -> None:
    """vmshost de un servicio sin permiso deja rollback-request.json; el actualizador ejecuta la vuelta atrás."""
    site.factory.release("2.1.0", comps=("app",))
    eng = site.engine()
    assert eng.check().result == "update_ok"
    # simula una versión que quedó a prueba (p. ej. corte justo antes de confirmar)
    ptr = site.pointer()
    PointerStore(site.layout.pointer_file).write(ptr.model_copy(update={"trial": True, "trial_since_unix": 1}))
    site.layout.rollback_request_file.write_text(json.dumps({"service": "VMSBackend", "reason": "3 caídas en 10 min"}))
    out = eng.handle_rollback_request()
    assert out is not None and out.result == "rollback_ok"
    assert site.pointer().active == "2.0.0" and not site.pointer().trial
    assert not site.layout.rollback_request_file.exists()
    assert Blacklist(site.layout.blacklist_file).contains("2.1.0")


def test_journal_last_good_is_always_a_valid_installed_version(site: Site) -> None:
    site.factory.release("2.1.0", comps=("app",), broken=True)
    site.engine().check()
    j = JournalStore(site.layout.journal_file).read()
    assert j is not None and j.last_good == "2.0.0"
    assert (site.layout.version_dir(j.last_good) / "release.json").is_file()
    assert ActivePointer.model_validate(json.loads(site.layout.pointer_file.read_text())).active == "2.0.0"
