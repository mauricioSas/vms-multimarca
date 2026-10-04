"""Textos sin voseo: español neutro con tuteo (PLAN-V2 §4.1, CONTRATO §0).

Comprobable, no opinable: busca una lista cerrada de formas de voseo (con límites de palabra) en
todo lo que lee un usuario o un instalador: la interfaz web, el visor, el instalador, la
documentación, los LEEME y los textos de Python dirigidos al usuario.

Excepciones:
- En Markdown se ignora lo que va entre comillas invertidas (`así`): es como el plan y el contrato
  citan las formas prohibidas.
- Las pruebas (`tests/`) no se revisan: citan las formas para buscarlas.
La revisión humana del tono sigue, pero el criterio de «terminado» es esta prueba.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Formas de voseo (presente, imperativo y enclíticos frecuentes). Sin «elegí», «escribí», «abrí»
# ni «salí»: también son el pretérito de «yo» en español neutro y darían falsos positivos.
FORMS = (
    "vos", "sos", "tenés", "querés", "podés", "sabés", "hacés", "decís", "venís", "necesitás", "estás vos",
    "hacé", "poné", "fijate", "fijáte", "andá", "mirá", "probá", "usá", "cerrá", "instalá", "ingresá",
    "ejecutá", "revisá", "configurá", "agregá", "descargá", "seleccioná", "tocá", "escribila", "acordate",
    "hacelo", "ponelo", "probalo", "instalalo", "decime", "avisame", "contame", "esperá", "volvé",
)
VOSEO = re.compile(r"\b(" + "|".join(re.escape(f) for f in FORMS) + r")\b", re.IGNORECASE)
# «SOS» en mayúsculas (señal de socorro) no es voseo: «sos» solo cuenta en minúsculas.
_CASE_SENSITIVE = {"sos"}

SCAN_DIRS = ["vms", "analytics", "central", "deploy", "tools", "docs", "native", "distribution", "updater",
             "infra", "spikes"]
SCAN_FILES = ["LEEME.md", "PLAN.md"]
SUFFIXES = {".md", ".html", ".js", ".css", ".txt", ".py", ".ps1", ".sh", ".iss", ".isl", ".rs", ".json",
            ".yml", ".yaml", ".toml"}
SKIP_PARTS = {"__pycache__", "node_modules", "target", "vendor", ".tmp", "screenshots"}
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _files() -> list[Path]:
    out: list[Path] = [ROOT / f for f in SCAN_FILES if (ROOT / f).is_file()]
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        out += [p for p in sorted(base.rglob("*"))
                if p.is_file() and p.suffix in SUFFIXES and not SKIP_PARTS.intersection(p.parts)]
    return out


def voseo_in(text: str, *, markdown: bool = False) -> list[str]:
    if markdown:
        text = _INLINE_CODE.sub("``", text)
    hits = []
    for m in VOSEO.finditer(text):
        word = m.group(0)
        if word.lower() in _CASE_SENSITIVE and word != word.lower():
            continue
        line = text.count("\n", 0, m.start()) + 1
        hits.append(f"línea {line}: «{word}»")
    return hits


FILES = _files()


def test_scanner_finds_files() -> None:
    assert len(FILES) > 50, "la prueba no está revisando nada: ¿cambió la estructura de carpetas?"


def test_detector_catches_voseo_and_respects_exceptions() -> None:
    assert voseo_in("Si querés, tocá «Guardar»")
    assert voseo_in("Vos tenés que reiniciar")
    assert not voseo_in("Si quieres, toca «Guardar». Ayer elegí la opción B.")
    assert not voseo_in("Pulsa el botón SOS")
    assert not voseo_in("Formas prohibidas: `vos`, `tenés`.", markdown=True)
    assert voseo_in("Formas prohibidas: vos, `tenés`.", markdown=True)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_voseo(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pytest.skip("no es texto UTF-8")
    hits = voseo_in(text, markdown=path.suffix == ".md")
    assert not hits, f"voseo en {path.relative_to(ROOT)}: " + "; ".join(hits[:5])
