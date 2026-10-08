"""Las grabaciones llevan la hora de ESTE PC, no la de la cámara (CONTRATO §18.3).

Una cámara sin hora puesta (01-01-1970, como la del usuario tras un corte de luz) no debe estropear los nombres
de los segmentos ni la línea de tiempo: MediaMTX sella cada fotograma al recibirlo salvo con
`useAbsoluteTimestamp: true`. Era el valor por defecto sin escribirlo; ahora va fijado en `pathDefaults` (en el
YAML y en el PATCH en caliente) para que un cambio de versión o de configuración no lo cambie sin que nadie lo vea.
Comprobado con MediaMTX v1.21.1 `--validate-conf`: la clave es válida en `pathDefaults`.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from vms.core.interfaces import CameraSource
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.settings import VmsSettings
from vms.engine.mtx_config import attach_config, global_config, path_configs, path_defaults, write_config

SRC = CameraSource(camera_id="cam-12345678", name="Caja", main_url="rtsp://u:p@10.0.0.5:554/a",
                   sub_url="rtsp://u:p@10.0.0.5:554/b", record=True)


def test_recordings_use_pc_time_in_every_generated_config(settings: VmsSettings, tmp_path: Path) -> None:
    pd = path_defaults(RecordingSettings(), RetentionSettings(), tmp_path / "rec")   # también el PATCH en caliente
    assert pd["useAbsoluteTimestamp"] is False
    cfg = global_config(settings, RecordingSettings(), RetentionSettings(), tmp_path / "rec")
    assert cfg["pathDefaults"]["useAbsoluteTimestamp"] is False
    attach = attach_config(settings, RecordingSettings(), RetentionSettings(), tmp_path / "rec", path_configs(SRC))
    f = tmp_path / "mediamtx.yml"
    write_config(f, attach)
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert doc["pathDefaults"]["useAbsoluteTimestamp"] is False
    for name, conf in doc["paths"].items():
        assert conf.get("useAbsoluteTimestamp", False) is False, name
