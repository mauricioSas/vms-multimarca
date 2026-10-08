"""Genera mediamtx.yml y la configuración de cada ruta (docs/CONTRATO.md §4.3 y §13.10).

- Modo `child` (desarrollo y v1): el YAML NUNCA lleva credenciales; las rutas de las cámaras (cuyas URL
  de origen llevan usuario y contraseña) se registran por la API de control de MediaMTX y solo viven en
  la memoria del proceso.
- Modo `attach` (Windows, motor como servicio `VMSEngine`): el YAML es la fuente única de las rutas y sí
  lleva las URL con contraseña (`attach_config`). Lo protege la ACL de `mediamtx\\` (solo VMSEngine,
  VMSBackend, SYSTEM y Administradores) y nunca entra en `vmsctl diag bundle`.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from vms.core.atomic import atomic_write_text
from vms.core.interfaces import CameraSource
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.mtx_auth import MtxCredentials
from vms.core.naming import is_valid_id, mtx_path
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
        # Métricas y pprof siempre apagados: nadie los lee y sus puertos por defecto (9998 y 9999) son los
        # de HikCentral (SYS) en el mismo PC. Con el 9998 ocupado, MediaMTX se cerraba al arrancar: es lo
        # primero que abre. Sin `metricsAddress` ni `pprofAddress`: un .env antiguo no los vuelve a encender.
        "metrics": False,
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


# --------------------------------------------------------------------------- modo attach (CONTRATO §13.10)
ATTACH_HEADER = (
    "# Generado por VMS Multimarca (motor como servicio, modo attach). No lo edites: se sobrescribe.\n"
    "# Es la FUENTE ÚNICA de las rutas: MediaMTX graba con él aunque el backend esté parado.\n"
    "# Contiene las URL de las cámaras con su contraseña: solo lo leen VMSEngine, VMSBackend, SYSTEM y\n"
    "# Administradores (vmsctl acl apply). No lo copies ni lo adjuntes a una incidencia.\n"
)


def attach_config(settings: VmsSettings, recording: RecordingSettings, retention: RetentionSettings,
                  recordings_dir: str | Path, paths: dict[str, dict[str, Any]], *,
                  creds: MtxCredentials | None = None) -> dict[str, Any]:
    """Documento completo para el modo attach: lo de `global_config` más las rutas de las cámaras."""
    cfg = global_config(settings, recording, retention, recordings_dir, creds=creds)
    cfg["paths"] = {name: paths[name] for name in sorted(paths)}
    return cfg


def render_attach(cfg: dict[str, Any]) -> str:
    return ATTACH_HEADER + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, default_flow_style=False)


def write_if_changed(file: Path, text: str) -> bool:
    """Escribe con `atomic_write` solo si el contenido cambia (cada escritura hace que MediaMTX recargue).
    Devuelve True si lo escribió."""
    try:
        if file.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError):
        pass  # ilegible o dañado: se reescribe
    atomic_write_text(file, text)
    return True


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


RETENTION_ONLY_CONF: dict[str, Any] = {"source": "publisher", "record": False}


def retention_paths(recordings_dir: str | Path, active: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Rutas inertes para las carpetas de grabación de cámaras que no están en `active` (CONTRATO §4.3).

    MediaMTX solo borra por antigüedad (`recordDeleteAfter`) y solo deja reproducir las rutas que tiene
    configuradas. Una cámara borrada, desactivada o en pausa por contraseña rechazada sale de la configuración y
    sus grabaciones se quedaban para siempre en disco (RGPD) y fuera de Reproducción. Estas rutas no tienen origen
    ni graban (`source: publisher` sin ningún usuario con permiso `publish`), pero heredan `recordPath` y
    `recordDeleteAfter` de `pathDefaults`: el limpiador y /list las cubren. Cuando caduca todo, MediaMTX borra la
    carpeta vacía y la ruta desaparece en la siguiente escritura.

    Rutas con nombre fijo, no una ruta regexp comodín: comprobado con MediaMTX v1.21.1 en Windows, el limpiador no
    encuentra ningún segmento a través de una ruta regexp (ni con `~^.*$`), aunque /list sí funcione."""
    root = Path(recordings_dir).resolve()
    wanted = set(active)
    out: dict[str, dict[str, Any]] = {}
    try:
        entries = sorted(e.name for e in os.scandir(root) if e.is_dir())
    except OSError:
        return out
    for name in entries:
        if not is_valid_id(name):
            continue
        path = mtx_path(name, "main")
        if path in wanted:
            continue
        try:
            if (root / name / "main").is_dir():
                out[path] = dict(RETENTION_ONLY_CONF)
        except OSError:
            continue
    return out


def conf_hash(conf: dict[str, Any]) -> str:
    """Huella de una configuración de ruta (para saber si hay que reemplazarla). No reversible."""
    return hashlib.sha256(json.dumps(conf, sort_keys=True).encode("utf-8")).hexdigest()
