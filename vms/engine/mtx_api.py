"""Cliente de la API de control de MediaMTX v3 y de su servidor de reproducción (/list, /get).

Ojo: la API de control expone las URLs de origen CON credenciales; por eso solo escucha en
127.0.0.1 y este cliente nunca registra cuerpos de respuesta de /v3/config/paths.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from vms.core.errors import EngineUnavailable
from vms.core.interfaces import RecordingSpan

log = logging.getLogger("vms.engine.api")

_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


class MtxApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"MediaMTX API {status}: {message}")
        self.status = status
        self.message = message


def parse_time(value: str) -> datetime:
    """RFC 3339 de Go (hasta 9 decimales, «Z» u offset) → datetime en UTC."""
    v = _FRACTION_RE.sub(r"\1", value.strip()).replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _q(name: str) -> str:
    return quote(name, safe="/")


class MediaMtxApi:
    def __init__(self, api_base: str, playback_base: str | None, *, timeout: float = 5.0,
                 auth: tuple[str, str] | None = None) -> None:
        self.api_base = api_base
        self.playback_base = playback_base
        self.client = httpx.AsyncClient(base_url=api_base, timeout=timeout, auth=auth)
        self.playback = (httpx.AsyncClient(base_url=playback_base, timeout=timeout, auth=auth)
                         if playback_base else None)

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.playback is not None:
            await self.playback.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise EngineUnavailable("El motor de vídeo (MediaMTX) no responde",
                                    details={"reason": type(exc).__name__}) from exc
        if resp.status_code >= 400:
            try:
                msg = str(resp.json().get("error", resp.text))
            except ValueError:
                msg = resp.text[:200]
            raise MtxApiError(resp.status_code, msg)
        return resp

    # ------------------------------------------------------------------ información y estado
    async def info(self) -> dict[str, Any]:
        return (await self._request("GET", "/v3/info")).json()

    async def _paged(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            data = (await self._request("GET", url, params={"itemsPerPage": 500, "page": page})).json()
            items.extend(data.get("items") or [])
            page += 1
            if page >= int(data.get("pageCount") or 1):
                return items

    async def paths_list(self) -> list[dict[str, Any]]:
        return await self._paged("/v3/paths/list")

    async def config_path_names(self) -> set[str]:
        return {str(i.get("name")) for i in await self._paged("/v3/config/paths/list")}

    # ------------------------------------------------------------------ configuración
    async def add_path(self, name: str, conf: dict[str, Any]) -> None:
        await self._request("POST", f"/v3/config/paths/add/{_q(name)}", json=conf)

    async def replace_path(self, name: str, conf: dict[str, Any]) -> None:
        await self._request("POST", f"/v3/config/paths/replace/{_q(name)}", json=conf)

    async def delete_path(self, name: str) -> bool:
        try:
            await self._request("DELETE", f"/v3/config/paths/delete/{_q(name)}")
            return True
        except MtxApiError as exc:
            if exc.status == 404:
                return False
            raise

    async def patch_path_defaults(self, conf: dict[str, Any]) -> None:
        await self._request("PATCH", "/v3/config/pathdefaults/patch", json=conf)

    # ------------------------------------------------------------------ reproducción
    async def list_recordings(self, path: str, start: datetime | None, end: datetime | None) -> list[RecordingSpan]:
        if self.playback is None:
            raise EngineUnavailable("La reproducción está desactivada (VMS_MTX_PLAYBACK_ADDRESS vacío)")
        params: dict[str, str] = {"path": path}
        if start is not None:
            params["start"] = format_time(start)
        if end is not None:
            params["end"] = format_time(end)
        try:
            resp = await self.playback.get("/list", params=params)
        except httpx.HTTPError as exc:
            raise EngineUnavailable("El servidor de reproducción no responde",
                                    details={"reason": type(exc).__name__}) from exc
        if resp.status_code in (400, 404):
            # Sin grabaciones todavía (carpeta inexistente, ruta sin segmentos o fuera de rango)
            log.debug("Sin grabaciones para %s: %s", path, resp.text[:200])
            return []
        if resp.status_code >= 400:
            raise EngineUnavailable(f"El servidor de reproducción respondió {resp.status_code}")
        spans: list[RecordingSpan] = []
        for item in resp.json() or []:
            try:
                spans.append(RecordingSpan(start=parse_time(str(item["start"])), duration=float(item["duration"])))
            except (KeyError, ValueError, TypeError):
                log.warning("Tramo de grabación con formato inesperado: %r", item)
        spans.sort(key=lambda s: s.start)
        return spans
