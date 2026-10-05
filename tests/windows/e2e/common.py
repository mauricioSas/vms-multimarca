"""Datos y comprobaciones que comparten los pasos del e2e de Windows."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.windows import harness as h
from tests.windows.conftest import E2E

#: Contraseña con lo que más se rompe al escribir un .env (comillas, barra, almohadilla, dólar, eñe).
ADMIN_PASSWORD = "Clave'e2e $X\\#ñ1"
SITE_TOKEN = "tok-e2e-0123456789"


def recordings_root() -> Path:
    """Disco de datos del runner (D: en windows-latest) o, si no existe, C:."""
    for drive in ("D:\\", "C:\\"):
        if Path(drive).exists():
            return Path(drive) / "vms-e2e"
    return Path(os.environ.get("TEMP", "C:\\")) / "vms-e2e"


def write_inf(path: Path, values: dict[str, str]) -> Path:
    """.inf en UTF-8 (sin BOM): así se prueba también la corrección de tildes de InfGet."""
    lines = ["[Setup]", *[f"{k}={v}" for k, v in values.items()]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path


def write_secrets(path: Path, **values: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return path


def store_inf(e2e: E2E, name: str = "ci.inf") -> tuple[Path, Path]:
    rec = recordings_root() / "Grabaciones CCTV"
    inf = write_inf(e2e.work / name, {
        "SetupType": "store", "Tasks": "storeviewer", "RecordingsDir": str(rec), "Cameras": "8",
        "MbpsPerCamera": "2,5", "SiteName": "Tienda Peñalara e2e", "SiteCode": "E2E-01", "SiteId": "",
        "CentralUrl": "https://central.e2e.local:8700", "Https": "1", "DomainProfile": "1", "HttpPort": "8600",
        "HttpsPort": "8643", "SetPrivateNetwork": "0", "UpdateSource": "http://127.0.0.1:8765/",
    })
    return inf, rec


def new_secrets(e2e: E2E, name: str = "ci-secrets.json") -> Path:
    return write_secrets(e2e.work / name, admin_password=ADMIN_PASSWORD, site_token=SITE_TOKEN)


def calls(e2e: E2E) -> list[dict[str, object]]:
    return h.read_calls(e2e.env.calls_log)


def commands_since(e2e: E2E, start: int) -> list[str]:
    return [h.command_of(c["argv"]) for c in calls(e2e)[start:]]  # type: ignore[arg-type]


def installed_vmsctl(e2e: E2E) -> Path:
    return h.program_dir() / "versions" / e2e.version / "bin" / "vmsctl.exe"


def assert_clean_exit(result: h.RunResult, expected: int = 0) -> str:
    text = result.text()
    assert result.code == expected, f"código {result.code} (esperado {expected}); registro:\n{text[-6000:]}"
    return text
