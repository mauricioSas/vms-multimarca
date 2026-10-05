"""La madurez de cada driver coincide con `docs/COMPATIBILIDAD.md` y con sus pruebas (PLAN-V2 §3.1 y §3.3).

- Cada driver del registro tiene su fila en «Matriz de drivers» con la misma madurez (y no hay filas de más).
- `verified` exige una fila **superada** en «Pruebas de 72 h» con ese driver.
- `fixtures` (o más) exige al menos una carpeta de fixtures capturada de un equipo real (`synthetic: false`).
- El resumen publicable («N verificados, N probados…») cuadra con el registro.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from vms.vendors.capture import iter_fixture_dirs
from vms.vendors.registry import MATURITY_ORDER, REGISTRY

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "COMPATIBILIDAD.md").read_text(encoding="utf-8")
FIXTURES = ROOT / "tests" / "vendors" / "fixtures"


def _section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", DOC, re.MULTILINE | re.DOTALL)
    assert m, f"falta la sección «{title}» en docs/COMPATIBILIDAD.md"
    return m.group(1)


def _rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and not re.match(r"^\|\s*-", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:]   # sin la cabecera


def doc_maturity() -> dict[str, str]:
    out: dict[str, str] = {}
    for cells in _rows(_section("Matriz de drivers")):
        m_id = re.fullmatch(r"`([a-z0-9-]+)`", cells[0])
        m_mat = re.search(r"`(verified|fixtures|community|experimental)`", cells[4]) if len(cells) > 4 else None
        assert m_id and m_mat, f"fila mal formada: {cells}"
        out[m_id.group(1)] = m_mat.group(1)
    return out


def test_every_driver_has_its_row_with_the_same_maturity() -> None:
    doc = doc_maturity()
    assert sorted(doc) == sorted(REGISTRY), "drivers sin fila o filas sin driver en docs/COMPATIBILIDAD.md"
    for vid, spec in REGISTRY.items():
        assert doc[vid] == spec.maturity, f"{vid}: el driver dice {spec.maturity} y la matriz {doc[vid]}"


def test_verified_needs_a_passed_72h_row() -> None:
    passed = {re.sub(r"[`\s]", "", cells[0]) for cells in _rows(_section("Pruebas de 72 h"))
              if len(cells) > 4 and "superada" in cells[4].lower()}
    for vid, spec in REGISTRY.items():
        if spec.maturity == "verified":
            assert vid in passed, f"{vid} dice «verified» sin una prueba de 72 h superada"


def test_fixtures_maturity_needs_real_fixtures() -> None:
    real: Counter[str] = Counter()
    for folder in iter_fixture_dirs(FIXTURES):
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        if meta.get("synthetic") is False:
            real[meta["driver"]] += 1
    for vid, spec in REGISTRY.items():
        if MATURITY_ORDER[spec.maturity] >= MATURITY_ORDER["fixtures"]:
            assert real[vid] >= 1, f"{vid} dice «{spec.maturity}» sin fixtures de un equipo real"


def test_published_summary_matches_registry() -> None:
    counts = Counter(s.maturity for s in REGISTRY.values())
    m = re.search(r"\*\*Resumen para publicar:\*\* (\d+) verificados con hardware, (\d+) probados con respuestas "
                  r"reales, (\d+) según\s+documentación pública y (\d+) experimentales", DOC)
    assert m, "falta el resumen publicable"
    assert tuple(int(x) for x in m.groups()) == (counts["verified"], counts["fixtures"], counts["community"],
                                                 counts["experimental"])
