"""Lógica del orquestador de cortes de luz (plan, veredicto e informe) con un doble de Hyper-V."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.windows.powercut.__main__ import main
from tests.windows.powercut.runner import STATES, Observation, judge, markdown, plan, report, run_one, write


def test_plan_covers_every_state_three_times() -> None:
    steps = plan(list(STATES), 3)
    assert len(steps) == 30 and steps[0] == ("downloaded", 1) and steps[-1] == ("rolled_back", 3)
    with pytest.raises(ValueError):
        plan(["inventado"], 1)


def good(**kw: object) -> Observation:
    o = Observation("switched", 1, announced=True, pointer={"active": "2.0.1", "trial": False},
                    journal={"state": "good"}, config_valid=True, services_running=True, recording=8)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


@pytest.mark.parametrize("change,reason", [
    ({}, None),
    ({"pointer": None}, "active.json"),
    ({"pointer": {"active": "2.0.0", "trial": True}}, "a prueba"),
    ({"pointer": {"active": "1.9.9"}}, "inesperada"),
    ({"journal": {"state": "switched"}}, "quedó en"),
    ({"config_valid": False}, "config.json"),
    ({"services_running": False}, "servicios"),
    ({"recording": 3}, "graban 3"),
    ({"announced": False}, "anunciar"),
])
def test_judge(change: dict[str, object], reason: str | None) -> None:
    o = judge(good(**change), from_version="2.0.0", to_version="2.0.1", min_recording=8)
    if reason is None:
        assert o.verdict == "ok" and o.reasons == []
    else:
        assert o.verdict == "fail" and any(reason in r for r in o.reasons)


class FakeVm:
    """La VM «se corta» y al volver el actualizador termina en la versión nueva."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.cut = False
        self.files = {"public-status.json": {"paused_at": None}, "journal.json": {"state": "switched"},
                      "active.json": {"active": "2.0.1", "trial": True}, "config.json": {"version": 2}}

    def restore(self, checkpoint: str) -> None:
        self.log.append(f"restore {checkpoint}")

    def start(self) -> None:
        self.log.append("start")
        if self.cut:
            self.files["journal.json"] = {"state": "good"}
            self.files["active.json"] = {"active": "2.0.1", "trial": False}

    def power_cut(self) -> None:
        self.log.append("cut")
        self.cut = True

    def guest(self, script: str, timeout_s: float = 120) -> str:
        if "SetEnvironmentVariable" in script:
            self.files["public-status.json"] = {"paused_at": "switched"}
            return ""
        for name, data in self.files.items():
            if name in script:
                return json.dumps(data)
        if "Get-Service" in script:
            return "0\n"
        if "health/deep" in script:
            return json.dumps({"cameras_recording": 8})
        return ""


def test_run_one_with_fake_vm(tmp_path: Path) -> None:
    vm = FakeVm()
    o = run_one(vm, checkpoint="limpio", state="switched", repeat=1, source="http://x/", wait_min=0.01,
                sleep=lambda s: None)
    assert vm.log == ["restore limpio", "start", "cut", "start"]
    assert o.verdict == "ok", o.reasons
    rep = report([o])
    assert rep["all_ok"] and rep["total"] == 1
    write(rep, tmp_path / "r.json")
    assert json.loads((tmp_path / "r.json").read_text())["ok"] == 1
    assert "| switched | 1 | ok | 2.0.1 | good |" in markdown(rep)


def test_cli_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--vm", "x", "--checkpoint", "c", "--source", "http://x/", "--dry-run"]) == 0
    assert "30 cortes" in capsys.readouterr().out
