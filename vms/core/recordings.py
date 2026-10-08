"""Grabaciones de MediaMTX: dónde están y qué significan sus errores (fuente única para el motor y la API).

- Carpeta: `mtx_config.RECORD_FILE_PATTERN` empieza por `%path/`, así que los segmentos de «<cámara>/main» van en
  `<grabaciones>/<cámara>/main/`. Se resuelve igual que `mtx_config.record_path` (`resolve()`): se mira la misma
  carpeta que escribe MediaMTX.
- Errores del servidor de reproducción (/list y /get) de MediaMTX v1.21.1, comprobados con el binario real en
  Windows: carpeta de la cámara inexistente → 400 «GetFileAttributesEx <carpeta>: The system cannot find the path
  specified.» (y lo registra como ERR); carpeta vacía o nada en el rango → 404 «no recording segments found»; ruta
  no configurada → 400 «path 'x' is not configured»; carpeta que el motor no puede leer → 400 «open <carpeta>:
  Access is denied.»; sin credenciales → 401; motor parado → error de conexión. Solo las dos primeras son «no hay
  grabaciones». Go pide a Windows los textos en inglés, pero puede caer en el idioma del sistema: por eso también
  van las formas en español.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from .errors import EngineError
from .naming import mtx_path

RecordingsPresence = Literal["no_root", "none", "present", "unknown"]

# Los «Qué hacer:» solo nombran pantallas y botones que existen en la web (tests/api/test_recordings_texts.py lo
# comprueba): no hay pantalla «Ajustes». Estado del sistema dice por qué no funciona el motor y qué hacer (el
# aviso de arriba y la tarjeta «Motor de vídeo», que puede poner «No arranca», «Arrancando», «Sin conexión»…, no
# solo «Detenido»): se manda allí sin citar una sola de esas palabras.
PLAYBACK_ENGINE_DOWN = ("El motor de vídeo no responde, así que no se pueden consultar las grabaciones. Qué hacer: "
                        "abre «Estado del sistema», que arriba dice por qué no funciona el motor y qué hacer. Si no "
                        "lo dice, reinicia el PC y, si sigue igual, avisa a soporte.")
PLAYBACK_DISABLED = ("La reproducción está desactivada en la configuración de este PC. Qué hacer: avisa a soporte para "
                     "que la active (ajuste VMS_MTX_PLAYBACK_ADDRESS).")
PLAYBACK_NOT_CONFIGURED = ("El motor de vídeo aún no tiene dada de alta esta cámara. Qué hacer: espera unos segundos "
                           "y pulsa «Actualizar»; si sigue igual, reinicia el PC.")
PLAYBACK_DENIED = ("El motor de vídeo no tiene permiso para leer la carpeta de grabaciones. Qué hacer: vuelve a "
                   "ejecutar el instalador del programa, que repara los permisos de las carpetas; si sigue igual, "
                   "avisa a soporte.")
PLAYBACK_AUTH = ("El motor de vídeo rechazó la consulta de grabaciones. Qué hacer: reinicia el PC para que los "
                 "servicios del programa se vuelvan a sincronizar; si sigue igual, avisa a soporte.")
PLAYBACK_FAILED = ("El motor de vídeo no pudo leer las grabaciones de esta cámara. Qué hacer: pulsa «Actualizar»; si "
                   "sigue igual, reinicia el PC y, si no se arregla, avisa a soporte.")
# La carpeta no se cambia desde la web (la pone el instalador), pero «Estado del sistema» dice cuál es.
RECORDINGS_DIR_MISSING = ("No se encuentra la carpeta de grabaciones. Qué hacer: si guardas las grabaciones en un "
                          "disco externo, conéctalo; si no, mira en «Estado del sistema» qué «Carpeta de grabaciones» "
                          "usa el programa y comprueba que existe. Si sigue igual, avisa a soporte.")

_MISSING_DIR_RE = re.compile(r"cannot find the (?:path|file) specified|no such file or directory"
                             r"|no puede encontrar (?:la ruta|el archivo) especificad[oa]", re.IGNORECASE)
_NOT_CONFIGURED_RE = re.compile(r"path '[^']*' is not configured", re.IGNORECASE)
_DENIED_RE = re.compile(r"access is denied|acceso denegado|permission denied", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------- carpetas
def record_dir(recordings_dir: str | Path, camera_id: str, stream: Literal["main", "sub"] = "main") -> Path:
    """Carpeta de los segmentos de una cámara. ValueError si el identificador no es válido."""
    return Path(recordings_dir).resolve().joinpath(*mtx_path(camera_id, stream).split("/"))


def recordings_presence(recordings_dir: str | Path, camera_id: str) -> RecordingsPresence:
    """¿Hay carpeta de grabaciones de la cámara?

    - `no_root`: no existe la carpeta de grabaciones entera (disco externo desconectado, carpeta borrada). Va
      primero porque, sin ella, la de la cámara tampoco existe y no se puede decir «nunca ha grabado».
    - `none`: la carpeta general existe, la de la cámara no: nunca ha grabado (o caducó todo).
    - `present`: hay carpeta (puede estar vacía).
    - `unknown`: no se pudo mirar (p. ej. acceso denegado). Quien llama debe preguntar a MediaMTX, que dará el
      error real.

    Ojo: en Windows `Path.is_dir()` devuelve False sin error si falta la unidad (WinError 21/123/1921), por eso la
    carpeta general se comprueba aparte."""
    try:
        root = Path(recordings_dir).resolve()
        if not root.is_dir():
            return "no_root"
        return "present" if record_dir(root, camera_id).is_dir() else "none"
    except (OSError, ValueError):
        return "unknown"


# ---------------------------------------------------------------------------------------------- errores de MediaMTX
def playback_error_text(body: bytes | str) -> str:
    """Texto de error de una respuesta de MediaMTX: el campo «error» del JSON o, si no es JSON, el cuerpo tal cual.

    El /list y el /get de MediaMTX devuelven rutas de carpetas en el error, nunca credenciales."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()
    if isinstance(data, dict) and data.get("error") is not None:
        return str(data["error"]).strip()
    return text.strip()


def playback_says_no_recordings(status: int, error: str) -> bool:
    """¿La respuesta significa «no hay grabaciones» (carpeta inexistente o sin segmentos en el rango)?"""
    return status == 404 or (status == 400 and _MISSING_DIR_RE.search(error) is not None)


def classify_playback_error(status: int, error: str) -> EngineError | None:
    """None si es «no hay grabaciones»; si no, el error para el usuario (`details.kind` dice cuál).

    `kind`: `not_configured` (el motor no tiene la ruta), `denied` (permisos de la carpeta), `auth` (credenciales
    internas), `other` (cualquier otro 4xx/5xx; en /get un 400 así es una petición no válida)."""
    if playback_says_no_recordings(status, error):
        return None
    details: dict[str, object] = {"status": status, "reason": error[:200]}
    if _NOT_CONFIGURED_RE.search(error):
        return EngineError(PLAYBACK_NOT_CONFIGURED, details={**details, "kind": "not_configured"})
    if _DENIED_RE.search(error):
        return EngineError(PLAYBACK_DENIED, details={**details, "kind": "denied"})
    if status in (401, 403):
        return EngineError(PLAYBACK_AUTH, details={**details, "kind": "auth"})
    return EngineError(PLAYBACK_FAILED, details={**details, "kind": "other"})
