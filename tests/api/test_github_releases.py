"""«Actualizaciones» de la web sin fuente TUF: versión nueva en GitHub Releases (mismas reglas que el visor)."""
from __future__ import annotations

from typing import Any

import pytest

from vms.api import github_releases as gh
from vms.api.routes import updates

P = gh.DOWNLOAD_PREFIX


def rel(tag: str, pre: bool, exe: bool = True) -> dict[str, Any]:
    v = tag.lstrip("v")
    assets = [{"name": "SHA256SUMS.txt", "browser_download_url": f"{P}{tag}/SHA256SUMS.txt"}]
    if exe:
        assets.append({"name": f"VMSMultimarca-Setup-{v}.exe", "browser_download_url": f"{P}{tag}/x.exe"})
    return {"tag_name": tag, "prerelease": pre, "draft": False, "assets": assets,
            "html_url": f"https://github.com/mauricioSas/vms-multimarca/releases/tag/{tag}"}


def test_newer_semver_with_prereleases() -> None:
    assert gh.newer("2.0.0-beta.10", "2.0.0-beta.9")
    assert gh.newer("2.0.0", "2.0.0-beta.9")
    assert not gh.newer("2.0.0-beta.2", "2.0.0-beta.2")
    assert not gh.newer("2.0.0-beta.1", "2.0.0-beta.2")


def test_pick_follows_the_viewer_rules() -> None:
    data = [rel("v2.0.0-beta.5", True, exe=False), rel("v2.0.0-beta.4", True), rel("v2.0.0-beta.3", True)]
    assert gh.pick(data, "2.0.0-beta.2")["version"] == "2.0.0-beta.4"  # type: ignore[index]
    assert gh.pick(data, "2.0.0-beta.4") is None
    assert gh.pick(data, "1.9.0") is None                       # una final no ve betas
    bad = rel("v2.0.0-beta.6", True)
    bad["assets"][1]["browser_download_url"] = "https://evil.example/x.exe"
    assert gh.pick([bad], "2.0.0-beta.2") is None


async def test_status_without_tuf_source_shows_the_github_version(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_latest(installed: str) -> dict[str, Any]:
        return {"ok": True, "new": {"version": "2.0.0-beta.4", "download_url": f"{P}x.exe", "notes_url": ""}}

    monkeypatch.setattr(gh, "latest", fake_latest)
    body = {"available_status": True, "running": "2.0.0-beta.2", "last_result": "error",
            "message_es": "No hay fuente de actualizaciones configurada (VMS_UPDATE_SOURCE)"}
    assert updates._without_tuf_source(body)
    out = await updates.github_status(body)
    assert out["last_result"] == "github_new" and out["available"] == "2.0.0-beta.4"
    assert "VMS_UPDATE_SOURCE" not in out["message_es"] and "icono de VMS" in out["message_es"]

    async def offline(installed: str) -> dict[str, Any]:
        return {"ok": False, "new": None}

    monkeypatch.setattr(gh, "latest", offline)
    out = await updates.github_status(body)
    assert out["last_result"] == "none" and "Internet" in out["message_es"]
    assert not updates._without_tuf_source({"available_status": True, "message_es": "Al día"})
