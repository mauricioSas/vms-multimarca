"""Proveedor `update` del latido (CONTRATO §7.3 bis y §15.6).

`payload_update(paths)` lee `updater\\public-status.json` y devuelve sus campos públicos. Síncrono y rápido:
solo lee un archivo local; nunca red, base de datos, IP ni secretos. Si no hay archivo, devuelve None (el
latido sale sin la clave). Solo usa la biblioteca estándar para que el backend y el agente lo importen sin
el runtime del actualizador.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIELDS = ("installed", "channel", "state", "hold", "window", "skipped", "last_check", "last_result", "message_es", "available",
          "metadata_expires", "clock_skew_s", "reboot_pending", "updated", "updater_version")


def _status_file(paths: Any) -> Path:
    base = getattr(paths, "base", None)
    if base is None:
        base = paths
    return Path(base) / "updater" / "public-status.json"


def payload_update(paths: Any) -> dict[str, Any] | None:
    try:
        raw = json.loads(_status_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    out = {k: raw.get(k) for k in FIELDS if k in raw}
    msg = out.get("message_es")
    if isinstance(msg, str) and len(msg) > 500:
        out["message_es"] = msg[:500]
    return out
