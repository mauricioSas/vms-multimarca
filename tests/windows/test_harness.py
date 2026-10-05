"""Partes portables del arnés del e2e de Windows (se prueban en cualquier sistema)."""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from tests.windows import harness as h
from tests.windows.wizard_capture import png_bytes, slug


def test_command_of_and_flags() -> None:
    argv = ["services", "install", "--role", "store", "--data-dir", r"C:\ProgramData\VMSMultimarca", "--json"]
    assert h.command_of(argv) == "services install"
    assert h.flag(argv, "role") == "store" and h.flag(argv, "nope") is None
    assert h.command_of(["migrate-from-v1", "--json"]) == "migrate-from-v1"
    assert h.command_of(["version", "switch", "2.0.0", "--json"]) == "version switch"
    assert h.command_of(["--json"]) == ""


def test_is_subsequence() -> None:
    hay = ["ports check", "version switch", "services install", "acl apply", "services start"]
    assert h.is_subsequence(["version switch", "services start"], hay)
    assert not h.is_subsequence(["services start", "version switch"], hay)
    assert h.is_subsequence([], hay)


def test_read_calls(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    log.write_text(json.dumps({"argv": ["kiosk", "rotate", "--json"], "exit": 0}) + "\n\n", encoding="utf-8")
    assert h.read_calls(log) == [{"argv": ["kiosk", "rotate", "--json"], "exit": 0}]
    assert h.read_calls(tmp_path / "no.jsonl") == []


def test_read_text_any(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes("ñ".encode("utf-16"))
    (tmp_path / "b").write_bytes(b"\xef\xbb\xbf" + "ñ".encode())
    assert h.read_text_any(tmp_path / "a") == "ñ" == h.read_text_any(tmp_path / "b")


def test_results_format_matches_system_check(tmp_path: Path) -> None:
    r = h.Results(installer="Setup.exe", mode="doubles", version="2.0.0")
    r.steps += [h.Step("2", "Instalación", ok=True, seconds=3.2), h.Step("5", "Visor", ok=None, notes=["B2"]),
                h.Step("11", "Desinstalar", ok=False)]
    r.save(tmp_path / "results-windows.json")
    data = json.loads((tmp_path / "results-windows.json").read_text(encoding="utf-8"))
    sistema = json.loads((Path(__file__).resolve().parents[1] / "e2e" / "results-sistema.json")
                         .read_text(encoding="utf-8"))
    assert set(sistema) <= set(data), "mismo formato que tests/e2e/results-sistema.json (PLAN-V2 §4.6 paso 12)"
    assert set(sistema["steps"][0]) == set(data["steps"][0])
    assert data["summary"] == {"ok": 1, "failed": 1, "skipped": 1}
    md = r.markdown()
    assert "| 2 | Instalación | **OK** |" in md and "omitido" in md and "**FALLO**" in md


def test_png_writer_produces_a_valid_png() -> None:
    w, h_ = 3, 2
    bgra = bytes([10, 20, 30, 255] * (w * h_))
    data = png_bytes(w, h_, bgra)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    ihdr = data[16:29]
    assert struct.unpack(">II", ihdr[:8]) == (3, 2)
    idat_len = struct.unpack(">I", data[33:37])[0]
    raw = zlib.decompress(data[41:41 + idat_len])
    assert raw[:5] == bytes([0, 30, 20, 10, 255])   # filtro 0 + RGBA (de BGRA)


@pytest.mark.parametrize(("text", "expected"), [("Tipo de puesto", "tipo-de-puesto"),
                                                ("Comprobación final", "comprobacion-final"), ("", "pagina")])
def test_slug(text: str, expected: str) -> None:
    assert slug(text) == expected


def test_e2e_order_leaves_room_for_b4(tmp_path: Path) -> None:
    from tests.windows.run_e2e import ordered_files

    (tmp_path / "e2e").mkdir()
    for name in ("test_00_v1_upgrade.py", "test_02_silent.py", "test_10_restart.py", "test_12_artifacts.py"):
        (tmp_path / "e2e" / name).write_text("", encoding="utf-8")
    (tmp_path / "test_update_06_app.py").write_text("", encoding="utf-8")
    names = [p.name for p in ordered_files(tmp_path)]
    assert names == ["test_00_v1_upgrade.py", "test_02_silent.py", "test_update_06_app.py", "test_10_restart.py",
                     "test_12_artifacts.py"]


def test_e2e_requirements_filter() -> None:
    from tests.windows.e2e_requirements import KEEP, lines

    found = lines(Path(__file__).resolve().parents[2] / "requirements-test.txt")
    names = {x.split("=")[0].split(">")[0].strip().lower() for x in found}
    assert names == KEEP
    assert not any("playwright" in x or "pgserver" in x for x in found)
