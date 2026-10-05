"""Migración v1 → v2 de los campos del equipo que necesita la auditoría (revisión v2, frente «operación»).

`Device.firmware_date` y `Device.manufacturer` son nuevos en la v2: un config.json de la v1 los recibe vacíos
(la v1 no guardaba la fecha del build ni el fabricante y no se pueden deducir), la auditoría dice «Desconocido»
hasta que el equipo se vuelva a leer, y una versión anterior de la v2 que no los conozca no los pierde al guardar.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vms.core import config_migrations as cm
from vms.core.models import AppConfig, Device

FIX = Path(__file__).parent / "fixtures"


def _v1() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / "config_v1_tienda.json").read_text(encoding="utf-8"))
    return data


def test_v1_devices_get_empty_firmware_date_and_manufacturer() -> None:
    doc, applied = cm.migrate(_v1())
    assert applied == [1]
    cfg = AppConfig.model_validate(doc)
    assert cfg.devices
    for dev in cfg.devices:
        assert dev.firmware_date == "" and dev.manufacturer == ""
    hik = next(d for d in cfg.devices if d.vendor == "hikvision")
    assert hik.firmware == "V4.30.085", "el firmware de la v1 se conserva tal cual (misma cadena que da la v2)"


def test_v2_fields_round_trip_and_survive_an_older_reader() -> None:
    doc, _ = cm.migrate(_v1())
    doc["devices"][0].update(firmware_date="2021-06-28", manufacturer="HIKVISION")
    cfg = AppConfig.model_validate(doc)
    assert (cfg.devices[0].firmware_date, cfg.devices[0].manufacturer) == ("2021-06-28", "HIKVISION")
    again = AppConfig.model_validate(json.loads(cfg.model_dump_json()))
    assert again.devices[0].firmware_date == "2021-06-28"
    # un lector que no conozca los campos (extra="allow") los conserva al reescribir
    raw = {k: v for k, v in doc["devices"][0].items()}
    kept = Device.model_validate(raw).model_dump(mode="json")
    assert kept["firmware_date"] == "2021-06-28" and kept["manufacturer"] == "HIKVISION"


def test_migrated_v1_hikvision_audit_is_unknown_not_ok() -> None:
    """Sin fecha de build (v1), la auditoría no se inventa un resultado: «Desconocido», nunca «Sin CVE»."""
    from vms.ops.security.advisories import version_table
    from vms.ops.security.audit import firmware_findings

    doc, _ = cm.migrate(_v1())
    dev = AppConfig.model_validate(doc).devices[0].model_copy(update={"model": "DS-2CD2143G2-I", "firmware": "V5.5.0"})
    found = firmware_findings(dev, version_table())
    assert found and all(f.status == "unknown" for f in found)
