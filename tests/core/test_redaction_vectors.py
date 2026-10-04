"""Vectores de ocultación de credenciales compartidos con Rust (PLAN-V2 §1.3, CONTRATO §13.6).

`tests/fixtures/redaction_vectors.json` lo leen esta prueba y `cargo test` de `native/common`, para
que la ocultación de Python (`vms.core.rtsp.redact`, registros del backend) y la de Rust (`vmsctl`,
que redacta la salida de MediaMTX) nunca diverjan.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from vms.core.logging_setup import RedactingFormatter
from vms.core.rtsp import redact

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "redaction_vectors.json")
                     .read_text(encoding="utf-8"))
CASES = VECTORS["cases"]


def test_vectors_file_is_well_formed() -> None:
    assert VECTORS["version"] == 1
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "nombres de caso repetidos"
    assert all({"name", "input", "expected"} <= set(c) for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_redact_matches_vector(case: dict[str, str]) -> None:
    assert redact(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_expected_output_never_contains_a_secret(case: dict[str, str]) -> None:
    for secret in VECTORS["secrets"]:
        assert secret not in case["expected"], f"el caso {case['name']} deja ver «{secret}»"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_log_formatter_applies_the_same_rules(case: dict[str, str]) -> None:
    record = logging.LogRecord("vms.test", logging.INFO, __file__, 1, "%s", (case["input"],), None)
    assert RedactingFormatter("%(message)s").format(record) == case["expected"]
