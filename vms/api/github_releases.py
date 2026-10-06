"""Versión nueva publicada en GitHub Releases, para la sección «Actualizaciones» de la web.

Sin fuente TUF (`VMS_UPDATE_SOURCE`, lo normal fuera del panel central) las versiones se publican en GitHub y se
instalan desde el visor (icono junto al reloj → «Descargar e instalar»). La web solo informa: el backend no
descarga ni instala nada. Mismas reglas que el visor (`github_update.rs`): una beta ve betas y finales, una final
solo finales, y solo cuenta una versión con su instalador y `SHA256SUMS.txt` en este repositorio.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger("vms.api.github_releases")

RELEASES_API = "https://api.github.com/repos/mauricioSas/vms-multimarca/releases?per_page=20"
DOWNLOAD_PREFIX = "https://github.com/mauricioSas/vms-multimarca/releases/download/"
CACHE_S = 3600.0
ERROR_CACHE_S = 300.0
_SAFE = re.compile(r"^[0-9A-Za-z.+-]{1,64}$")

_cache: tuple[float, str, dict[str, Any]] | None = None


def _key(v: str) -> tuple[tuple[int, ...], int, tuple[tuple[int, int | str], ...]]:
    v = v.lstrip("v").split("+", 1)[0]
    core, _, pre = v.partition("-")
    nums = tuple(int(p) if p.isdigit() else 0 for p in core.split("."))
    nums = nums + (0,) * (3 - len(nums)) if len(nums) < 3 else nums
    pre_parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return nums, 0 if pre else 1, pre_parts  # una final va después de sus betas


def newer(a: str, b: str) -> bool:
    """¿`a` es más nueva que `b`? (SemVer con pre-versiones: beta.10 > beta.9 y 2.0.0 > 2.0.0-beta.9)."""
    return _key(a) > _key(b)


def pick(releases: list[dict[str, Any]], installed: str) -> dict[str, str] | None:
    allow_pre = "-" in installed.split("+", 1)[0]
    best: dict[str, str] | None = None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft") or (r.get("prerelease") and not allow_pre):
            continue
        version = str(r.get("tag_name", "")).lstrip("v")
        if not _SAFE.match(version) or not newer(version, installed):
            continue
        if best is not None and not newer(version, best["version"]):
            continue
        assets = {a.get("name"): str(a.get("browser_download_url", "")) for a in r.get("assets") or []
                  if isinstance(a, dict)}
        exe = assets.get(f"VMSMultimarca-Setup-{version}.exe", "")
        if not exe.startswith(DOWNLOAD_PREFIX) or not assets.get("SHA256SUMS.txt", "").startswith(DOWNLOAD_PREFIX):
            continue
        notes = str(r.get("html_url", ""))
        best = {"version": version, "download_url": exe,
                "notes_url": notes if notes.startswith("https://github.com/") else ""}
    return best


async def latest(installed: str) -> dict[str, Any]:
    """{"ok": bool, "new": {...} | None}. Con caché: una consulta por hora (5 min si falló)."""
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[1] == installed and now < _cache[0]:
        return _cache[2]
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            r = await client.get(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                        "User-Agent": "VMSMultimarca"})
        r.raise_for_status()
        data = r.json()
        result: dict[str, Any] = {"ok": True, "new": pick(data if isinstance(data, list) else [], installed)}
        ttl = CACHE_S
    except (httpx.HTTPError, ValueError) as exc:
        log.info("No se pudo consultar GitHub Releases: %s", exc)
        result, ttl = {"ok": False, "new": None}, ERROR_CACHE_S
    _cache = (now + ttl, installed, result)
    return result


__all__ = ["latest", "newer", "pick"]
