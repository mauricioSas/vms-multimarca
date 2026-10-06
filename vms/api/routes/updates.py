"""Actualizaciones en el backend (CONTRATO §15.6). Dueño: B4.

- `GET /api/updates/status` (operador): estado de la actualización de la sede para `/status`, leído de
  `<datos>\\updater\\public-status.json` (lo escribe solo `VMSUpdater`; nunca lleva secretos).
- `GET /api/internal/health/deep` (token interno): lo que comprueba el actualizador tras aplicar una versión
  (PLAN-V2 §2.5 paso 7): estado, versión en marcha, motor, cámaras grabando y frescura de la analítica.
- Vigilante (lifespan del router): cuando cambia la versión instalada o el estado de la actualización,
  emite el evento SSE `update` (`publish_update`) para que el visor y la interfaz se enteren.

El backend no habla con el actualizador ni puede pedirle nada: el rollback y «buscar ahora» van por la tubería
elevada (`vmsctl update …`) o por el panel central.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from starlette.responses import Response

import vms
from vms import __version__

from ..deps import Principal, get_state, require_internal, require_operator
from ..errors import json_response
from ..events import UpdateEvent, publish_update
from ..state import AppState

log = logging.getLogger("vms.api.updates")

STATUS_FIELDS = ("installed", "channel", "state", "hold", "window", "skipped", "last_check", "last_result",
                 "message_es", "available", "metadata_expires", "clock_skew_s", "reboot_pending", "updated",
                 "updater_version")
WATCH_INTERVAL_S = 5.0


def _release_file() -> Path:
    return Path(vms.__file__).resolve().parents[2] / "release.json"


def is_installed_release() -> bool:
    """¿Corre el producto instalado (con `release.json`)? En desarrollo y pruebas no se consulta GitHub."""
    try:
        return _release_file().is_file()
    except (OSError, IndexError):
        return False


def release_version() -> str:
    """Versión del producto en marcha: la de `versions\\<X>\\release.json` si existe (instalación v2), si no
    `vms.__version__` (desarrollo)."""
    try:
        rel = _release_file()
        data = json.loads(rel.read_text(encoding="utf-8"))
        v = data.get("version")
        if isinstance(v, str) and v:
            return v
    except (OSError, ValueError, AttributeError, IndexError):
        pass
    return __version__


def read_public_status(base: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((base / "updater" / "public-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return {k: raw.get(k) for k in STATUS_FIELDS if k in raw}


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState | None = getattr(app.state, "vms", None)
    task: asyncio.Task[None] | None = None
    if state is not None:
        task = asyncio.create_task(_watch(state), name="updates-watch")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _watch(state: AppState, interval: float = WATCH_INTERVAL_S) -> None:
    last: tuple[Any, ...] | None = None
    while True:
        try:
            st = await asyncio.to_thread(read_public_status, state.paths.base)
            if st is not None:
                key = (st.get("installed"), st.get("state"), st.get("last_result"))
                if last is not None and key != last:
                    ev: UpdateEvent = {"version": str(st.get("installed") or ""), "state": str(st.get("state") or ""),
                                       "message_es": str(st.get("message_es") or ""),
                                       "viewer_restart": st.get("installed") != last[0]}
                    publish_update(state.bus, ev)
                last = key
        except Exception:  # noqa: BLE001 - el vigilante nunca tumba el backend
            log.exception("Error vigilando el estado de las actualizaciones")
        await asyncio.sleep(interval)


router = APIRouter(prefix="/api", tags=["updates"], lifespan=_lifespan)


def status_body(base: Path) -> dict[str, Any]:
    """Cuerpo de `GET /api/updates/status` (también lo usan las pruebas de la interfaz)."""
    st = read_public_status(base)
    running = release_version()
    if st is None:
        return {"available_status": False, "running": running, "installed": running, "state": "unknown",
                "last_result": "none", "message_es": "El actualizador todavía no ha informado en este equipo"}
    return {"available_status": True, "running": running, **st}


def _without_tuf_source(body: dict[str, Any]) -> bool:
    """Sin fuente TUF configurada (instalación normal): las versiones se publican en GitHub."""
    return "VMS_UPDATE_SOURCE" in str(body.get("message_es") or "") or not body.get("available_status")


async def github_status(body: dict[str, Any]) -> dict[str, Any]:
    from ..github_releases import latest

    running = str(body.get("running") or __version__)
    gh = await latest(running)
    out = {**body, "source": "github", "channel": "GitHub (beta)" if "-" in running else "GitHub",
           "last_check": None, "metadata_expires": None, "clock_skew_s": None, "window": None, "state": "idle"}
    if not gh["ok"]:
        out.update(last_result="none", available=None,
                   message_es="No se pudo consultar si hay versiones nuevas (hace falta Internet).")
    elif gh["new"]:
        new = gh["new"]
        out.update(last_result="github_new", available=new["version"], download_url=new["download_url"],
                   notes_url=new["notes_url"],
                   message_es=f"Hay una versión nueva: {new['version']}. Para instalarla, pulsa el icono de VMS "
                              "junto al reloj de Windows → «Versión nueva…: descargar e instalar». Se conservan "
                              "las cámaras, la configuración y las grabaciones.")
    else:
        out.update(last_result="no_update", available=None, message_es="")
    return out


@router.get("/updates/status")
async def updates_status(_: Principal = Depends(require_operator), state: AppState = Depends(get_state)) -> Response:
    body = await asyncio.to_thread(status_body, state.paths.base)
    if is_installed_release() and _without_tuf_source(body):
        body = await github_status(body)
    return json_response(body)


@router.get("/internal/health/deep", dependencies=[Depends(require_internal)])
async def health_deep(state: AppState = Depends(get_state)) -> Response:
    ov = await state.overview()
    engine = ov["engine"]
    enabled = [c for c in ov["cameras"] if c["enabled"]]
    an = ov["analytics"] if isinstance(ov["analytics"], dict) else {}
    analytics_on = state.analytics_enabled()
    age = None
    if an.get("updated_at"):
        from datetime import datetime, timezone
        try:
            upd = datetime.fromisoformat(str(an["updated_at"]).replace("Z", "+00:00"))
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=timezone.utc)
            age = round((datetime.now(timezone.utc) - upd).total_seconds(), 1)
        except ValueError:
            age = None
    return json_response({
        "status": ov["status"], "problems": ov["problems"], "version": __version__, "release": release_version(),
        "uptime_s": state.uptime_s(),
        "engine": {"running": bool(engine.running), "api_ok": bool(engine.api_ok)},
        "cameras_total": len(enabled), "cameras_recording": sum(1 for c in enabled if c["recording"]),
        "analytics": {"enabled": analytics_on, "running": bool(an.get("running")), "stale": bool(an.get("stale", True)),
                      "age_s": age},
        "config_read_only": bool(getattr(state.repo.store, "read_only", False)),
    })
