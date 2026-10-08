"""Los «Qué hacer:» de Reproducción y de «Grabando» llevan a sitios que existen.

Antes, tres textos mandaban a «Ajustes» (bajar los días de retención, revisar la carpeta de grabaciones), pero el
programa no tiene ninguna pantalla con ese nombre: los días solo se cambian en el asistente «Primeros pasos» y la
carpeta la pone el instalador. Un usuario que no es técnico no podía seguir el paso.

Aquí se comprueba, para cada texto de cara al usuario de esta parte, que tiene «Qué hacer:», que no nombra
«Ajustes» y que cada pantalla, sección o botón que cita entre comillas «…» aparece en la web (vms/web).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from vms.api.routes import recordings as routes
from vms.core import recordings as core
from vms.engine.engine import RECORDING_STALE_MSG, explain_recorder_error

WEB = Path(__file__).resolve().parents[2] / "vms" / "web"

# Un error «[recorder]» real de MediaMTX por cada explicación distinta
RECORDER_RAW = {
    "permiso": "open C:\\ProgramData\\VMSMultimarca\\recordings\\cam-1\\main\\x.mp4: Access is denied.",
    "disco lleno": "write D:\\VMS\\cam-1\\main\\x.mp4: There is not enough space on the disk.",
    "sin carpeta": "mkdir D:\\VMS: The system cannot find the path specified.",
    "otro": "algo raro",
}

TEXTS: dict[str, str] = {
    **{name: getattr(core, name) for name in ("PLAYBACK_ENGINE_DOWN", "PLAYBACK_DISABLED", "PLAYBACK_NOT_CONFIGURED",
                                              "PLAYBACK_DENIED", "PLAYBACK_AUTH", "PLAYBACK_FAILED",
                                              "RECORDINGS_DIR_MISSING")},
    **{name: getattr(routes, name) for name in ("NOTICE_DISABLED", "NOTICE_DEVICE_DISABLED", "NOTICE_RECORDING_OFF",
                                                "NOTICE_NEVER_RECORDED", "NO_RECORDING_THERE", "PLAYBACK_REJECTED")},
    "RECORDING_STALE_MSG": RECORDING_STALE_MSG,
    **{f"recorder: {k}": explain_recorder_error(raw).split(" (Detalle:")[0] for k, raw in RECORDER_RAW.items()},
}


def _web_text() -> str:
    files = [*WEB.glob("pages/*.html"), *WEB.glob("static/js/*.js"), WEB / "static" / "help" / "es.json"]
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def _labels(text: str) -> list[str]:
    return re.findall(r"«([^«»]+)»", text)


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_what_to_do_only_names_places_that_exist(name: str) -> None:
    text = TEXTS[name]
    assert "Qué hacer:" in text
    assert re.search(r"\bAjustes\b", text) is None, f"{name}: no existe ninguna pantalla «Ajustes»"
    web = _web_text()
    missing = [label for label in _labels(text) if label not in web]
    assert missing == [], f"{name} cita {missing}, que no aparecen en la web"


def test_full_disk_and_missing_folder_say_where_to_go() -> None:
    full = TEXTS["recorder: disco lleno"]
    assert all(x in _labels(full) for x in ("Equipos y monitores", "Ayuda", "Primeros pasos (asistente)",
                                            "Días de grabación"))
    for name in ("recorder: sin carpeta", "RECORDINGS_DIR_MISSING"):
        assert {"Estado del sistema", "Carpeta de grabaciones"} <= set(_labels(TEXTS[name]))


def test_engine_down_sends_to_the_reason_not_to_one_engine_label() -> None:
    """Estado del sistema ya dice por qué no funciona el motor, con su «Qué hacer:» (aviso #st-engine-problem), y
    la tarjeta «Motor de vídeo» puede poner «No arranca», «Arrancando», «Sin conexión» o «No responde». Antes el
    reproductor decía «si pone "Detenido", reinicia el PC»: con un puerto ocupado ponía «No arranca» y el usuario
    se quedaba sin paso que seguir."""
    text = core.PLAYBACK_ENGINE_DOWN
    status_js = (WEB / "static" / "js" / "status.js").read_text(encoding="utf-8")
    labels = set(re.findall(r'return "([^"]+)";', status_js)) | set(re.findall(r'"([A-Z][^"]+)"\]', status_js))
    assert "Detenido" in labels and "No arranca" in labels, "status.js ya no tiene las etiquetas del motor"
    assert "Estado del sistema" in _labels(text) and "por qué no funciona el motor" in text
    assert not [label for label in _labels(text) if label in labels], "no cites una sola etiqueta del motor"
    assert 'id="st-engine-problem"' in (WEB / "pages" / "status.html").read_text(encoding="utf-8")


def test_web_warnings_of_this_part_only_name_places_that_exist() -> None:
    """Los avisos escritos en la propia web (aviso rojo de Estado del sistema, reproductor)."""
    web = _web_text()
    for js in ("status.js", "playback.js"):
        source = (WEB / "static" / "js" / js).read_text(encoding="utf-8")
        for line in (ln for ln in source.splitlines() if "Qué hacer:" in ln):
            missing = [label for label in _labels(line) if label not in web]
            assert missing == [] and "Ajustes" not in line, f"{js}: {line.strip()[:120]}"
