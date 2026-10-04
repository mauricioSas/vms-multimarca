"""Genera mediamtx.yml y la configuración de cada ruta (docs/CONTRATO.md §4.3).

El YAML que se escribe en disco NUNCA lleva credenciales: las rutas de las cámaras (cuyas
URLs de origen llevan usuario y contraseña) se registran por la API de control de MediaMTX y
solo viven en la memoria del proceso.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from vms.core.atomic import atomic_write_text
from vms.core.interfaces import CameraSource
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.mtx_auth import MtxCredentials
from vms.core.naming import mtx_path
from vms.core.settings import VmsSettings

SUB_CLOSE_AFTER = "30s"
SUB_START_TIMEOUT = "10s"
# Hora local CON su desfase (%z → «+0200»): la noche del cambio de hora de octubre las 02:xx se
# repiten y, sin desfase, MediaMTX interpretaba la primera hora como la segunda (una hora de
# grabación desaparecía de la línea de tiempo). Las grabaciones antiguas sin desfase se renombran al
# arrancar el motor (disk_guard.migrate_legacy_names).
RECORD_FILE_PATTERN = "%path/%Y-%m-%d_%H-%M-%S-%f%z"


def split_address(address: str) -> tuple[str, int] | None:
    """«127.0.0.1:8554» → ("127.0.0.1", 8554); «:8189» → ("127.0.0.1", 8189); "" → None."""
    address = (address or "").strip()
    if not address:
        return None
    host, _, port = address.rpartition(":")
    host = host.strip("[]")
    if not host or host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return host, int(port)


def http_base(address: str) -> str | None:
    hp = split_address(address)
    if hp is None:
        return None
    host, port = hp
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def record_path(recordings_dir: str | Path) -> str:
    # Barras normales también en Windows: MediaMTX (Go) las acepta y evita escapar «\» en YAML.
    return f"{Path(recordings_dir).resolve().as_posix().rstrip('/')}/{RECORD_FILE_PATTERN}"


def path_defaults(recording: RecordingSettings, retention: RetentionSettings,
                  recordings_dir: str | Path) -> dict[str, Any]:
    return {
        "rtspTransport": "tcp",
        "record": False,
        "recordFormat": "fmp4",
        "recordPath": record_path(recordings_dir),
        "recordPartDuration": f"{int(recording.part_seconds)}s",
        "recordSegmentDuration": f"{int(recording.segment_seconds)}s",
        "recordDeleteAfter": f"{int(retention.days) * 24}h",
    }


def global_config(settings: VmsSettings, recording: RecordingSettings, retention: RetentionSettings,
                  recordings_dir: str | Path, *, creds: MtxCredentials | None = None) -> dict[str, Any]:
    """Documento completo de mediamtx.yml (sin rutas de cámaras ni credenciales en claro).

    `creds` = usuarios internos (vms.core.mtx_auth); por defecto, los derivados del token interno."""
    if creds is None:
        creds = MtxCredentials.from_internal_token(settings.ensure_internal_token())
    api = settings.mtx_api_address.strip()
    if not api:
        raise ValueError("VMS_MTX_API_ADDRESS no puede estar vacío: el motor necesita la API de MediaMTX")
    cfg: dict[str, Any] = {
        "logLevel": "info",
        "logDestinations": ["stdout"],
        "readTimeout": "10s",
        "writeTimeout": "10s",
        "authMethod": "internal",
        # Sin usuario anónimo: la API muestra las URL de origen con contraseñas y permite ejecutar
        # comandos. Solo los usuarios internos (hash SHA-256), solo desde el propio equipo y sin
        # «publish» (nadie puede inyectar vídeo en las rutas).
        "authInternalUsers": creds.internal_users(),
        "api": True,
        "apiAddress": api,
        "apiAllowOrigins": [],
        "metrics": bool(settings.mtx_metrics_address.strip()),
        "pprof": False,
        "playback": bool(settings.mtx_playback_address.strip()),
        "playbackAllowOrigins": [],
        "rtsp": bool(settings.mtx_rtsp_address.strip()),
        "rtspTransports": ["tcp"],
        "rtspEncryption": "no",
        "rtmp": False,
        "hls": False,
        "srt": False,
        "moq": False,
        "webrtc": bool(settings.mtx_webrtc_address.strip()),
        "webrtcAllowOrigins": [],
        "webrtcLocalUDPAddress": settings.mtx_webrtc_ice_udp.strip(),
        "webrtcLocalTCPAddress": settings.mtx_webrtc_ice_tcp.strip(),
        "webrtcIPsFromInterfaces": True,
        "webrtcAdditionalHosts": list(settings.mtx_webrtc_additional_hosts),
        "pathDefaults": path_defaults(recording, retention, recordings_dir),
        "paths": {},
    }
    if cfg["metrics"]:
        cfg["metricsAddress"] = settings.mtx_metrics_address.strip()
    if cfg["playback"]:
        cfg["playbackAddress"] = settings.mtx_playback_address.strip()
    if cfg["rtsp"]:
        cfg["rtspAddress"] = settings.mtx_rtsp_address.strip()
    if cfg["webrtc"]:
        cfg["webrtcAddress"] = settings.mtx_webrtc_address.strip()
    return cfg


def write_config(file: Path, cfg: dict[str, Any]) -> None:
    text = ("# Generado por VMS Multimarca en cada arranque. No lo edites: se sobrescribe.\n"
            "# Las rutas de las cámaras se registran por la API (sin credenciales en disco).\n"
            + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, default_flow_style=False))
    atomic_write_text(file, text)


def path_configs(src: CameraSource) -> dict[str, dict[str, Any]]:
    """{nombre_ruta: conf} de una cámara: principal (grabación 24/7) y subflujo bajo demanda."""
    out: dict[str, dict[str, Any]] = {
        mtx_path(src.camera_id, "main"): {
            "source": src.main_url,
            "rtspTransport": src.rtsp_transport,
            "sourceOnDemand": False,
            "record": bool(src.record),
        }
    }
    if src.sub_url:
        out[mtx_path(src.camera_id, "sub")] = {
            "source": src.sub_url,
            "rtspTransport": src.rtsp_transport,
            "sourceOnDemand": True,
            "sourceOnDemandStartTimeout": SUB_START_TIMEOUT,
            "sourceOnDemandCloseAfter": SUB_CLOSE_AFTER,
            "record": False,
        }
    return out


def conf_hash(conf: dict[str, Any]) -> str:
    """Huella de una configuración de ruta (para saber si hay que reemplazarla). No reversible."""
    return hashlib.sha256(json.dumps(conf, sort_keys=True).encode("utf-8")).hexdigest()
