"""Batería común de contrato: registro × fixtures (PLAN-V2 §3.3).

Cada carpeta `tests/vendors/fixtures/<driver>/<modelo>__<firmware>/` se reproduce con `ReplayTransport` (HTTP y
SOAP grabados) y `RtspReplayServer` (retos y SDP grabados). Para cada driver se comprueba:
1. `probe()` → modelo, firmware y serie no vacíos.
2. `list_channels()` → los canales de `meta.json` (número e ids), con códec cuando la API lo da.
3. Preset y canal → URL válida, credenciales codificadas %XX, sin «//» salvo que el driver lo declare.
4. SDP → códec correcto, y no falla si falta `rtpmap` o `sprop-*` (ni con saltos de línea LF).
5. Autenticación → elige el reto correcto; una contraseña mala da **un** solo intento; bloqueo ≠ contraseña mala.
6. `snapshot()` → JPEG válido sin escribir en disco.
7. Detección → `best_match(pistas de la fixture)` pone este driver primero con más de 0,7.
8. Ningún registro contiene la contraseña de prueba `Sim#Pass:1@/x`.

Las comprobaciones que no aplican a un driver no se generan (un perfil RTSP no tiene `probe()`), así el
resumen no se llena de omisiones.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

from tools.mocks.replay import ReplayTransport, RtspReplayServer
from vms.core import rtsp
from vms.core.errors import DeviceAuthFailed
from vms.core.interfaces import Capability, DetectionHints, DriverSpec
from vms.core.models import DeviceBase
from vms.vendors import client_for, has_api
from vms.vendors.auth import STRENGTH, parse_challenges
from vms.vendors.capture import iter_fixture_dirs
from vms.vendors.errors import DeviceLocked
from vms.vendors.registry import REGISTRY, best_match, preset_for
from vms.vendors.rtsp_probe import probe_rtsp
from vms.vendors.sdp import parse_sdp

PW = "Sim#Pass:1@/x"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FOLDERS = list(iter_fixture_dirs(FIXTURES))


def _meta(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "meta.json").read_text(encoding="utf-8"))


def _spec(folder: Path) -> DriverSpec:
    return REGISTRY[_meta(folder)["driver"]]


def _id(folder: Path) -> str:
    return f"{folder.parent.name}/{folder.name}"


def _cases(pred: Any) -> list[Any]:
    return [pytest.param(f, id=_id(f)) for f in FOLDERS if pred(f)]


def _has_http(f: Path) -> bool:
    return has_api(_spec(f)) and any((f / "http").glob("[0-9]*.json"))


def _has_sdp(f: Path) -> bool:
    return (f / "rtsp" / "main_ch1.sdp").is_file()


def _device(folder: Path, *, rtsp_port: int = 554, password_user: str = "admin") -> DeviceBase:
    meta = _meta(folder)
    return DeviceBase(name="fixture", vendor=meta["driver"], kind=meta["kind"] if meta["kind"] != "unknown" else "camera",
                      host="127.0.0.1", http_port=80, rtsp_port=rtsp_port, username=password_user)


@pytest.fixture(autouse=True)
def _no_password_in_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level(logging.DEBUG)
    yield
    assert PW not in caplog.text, "la contraseña de prueba apareció en un registro"


def test_every_fixture_belongs_to_a_registered_driver() -> None:
    assert FOLDERS, "no hay fixtures"
    for f in FOLDERS:
        meta = _meta(f)
        assert meta["driver"] in REGISTRY, f
        assert f.parent.name == meta["driver"], f
        assert meta["anonymized"] is True and "synthetic" in meta and "source" in meta


def test_every_driver_has_fixtures() -> None:
    """Añadir un driver = su archivo + fixtures: la batería tiene que poder ejercitarlo."""
    covered = {_meta(f)["driver"] for f in FOLDERS}
    assert sorted(set(REGISTRY) - covered) == []


# --------------------------------------------------------------------------- 1 y 2: probe y canales
@pytest.mark.parametrize("folder", _cases(_has_http))
async def test_probe_and_channels(folder: Path) -> None:
    meta = _meta(folder)
    replay = ReplayTransport(folder, PW)
    client = client_for(_device(folder), PW, transport=replay)
    try:
        info = await client.probe()
        channels = await client.list_channels()
    finally:
        await client.aclose()
    assert info.model and info.firmware and info.serial, info
    assert info.model == meta["model"] and info.serial == meta["serial"]
    assert len(channels) == meta["channels"] and [c.channel for c in channels] == meta["channel_ids"]
    assert any(c.main_codec for c in channels), "la API no informó de ningún códec"
    assert replay.unmatched == [], replay.unmatched


# --------------------------------------------------------------------------- 3: rutas y URL
@pytest.mark.parametrize("folder", _cases(lambda f: True))
def test_preset_urls_are_valid(folder: Path) -> None:
    meta, spec = _meta(folder), _spec(folder)
    ids = meta["channel_ids"] or [1]
    paths: list[str] = []
    for ch in ids:
        p = preset_for(spec.id, ch, meta["kind"])
        if p is not None:
            paths += [p.main] + ([p.sub] if p.sub else [])
    if not paths and meta["rtsp_path"]:
        paths = [meta["rtsp_path"]]
    if not paths:
        assert spec.presets is None and spec.client is None, "driver sin preset ni ruta grabada"
        return
    for path in paths:
        url = rtsp.build_rtsp_url("192.0.2.10", 554, path, "admin", PW)
        parts = urlsplit(url)
        assert parts.scheme == "rtsp" and parts.hostname == "192.0.2.10" and parts.port == 554
        assert parts.username == "admin" and unquote(parts.password or "") == PW
        assert "%23" in url and "%40" in url and "%2F" in url and "%3A" in url     # # @ / : codificados
        assert path.startswith("/")
        assert "//" not in parts.path or spec.double_slash, path
        assert spec.presets is None or "?" not in path or not preset_for(spec.id, 1, meta["kind"]).query_safe  # type: ignore[union-attr]


# --------------------------------------------------------------------------- 4: SDP
def _variants(sdp: str) -> list[str]:
    no_rtpmap = "\n".join(ln for ln in sdp.splitlines() if not ln.startswith("a=rtpmap"))
    no_sprop = "\n".join(ln.split(";sprop")[0] if ln.startswith("a=fmtp") else ln for ln in sdp.splitlines())
    return [sdp, sdp.replace("\r\n", "\n"), no_rtpmap, no_sprop]


@pytest.mark.parametrize("folder", _cases(_has_sdp))
def test_sdp_codec_and_tolerance(folder: Path) -> None:
    meta = _meta(folder)
    sdp = (folder / "rtsp" / "main_ch1.sdp").read_text(encoding="utf-8")
    assert parse_sdp(sdp).video_codec == meta["main_codec"]
    for v in _variants(sdp):
        info = parse_sdp(v)                      # nunca lanza
        if meta["main_codec"] in ("H.264", "H.265") and "a=fmtp" in v:
            assert info.video_codec in (meta["main_codec"], None)
    no_rtpmap = _variants(sdp)[2]
    if "sprop" in sdp:
        assert parse_sdp(no_rtpmap).video_codec == meta["main_codec"]    # el fmtp delata el códec


# --------------------------------------------------------------------------- 5: autenticación
@pytest.mark.parametrize("folder", _cases(_has_http))
async def test_api_wrong_password_is_one_attempt(folder: Path) -> None:
    replay = ReplayTransport(folder, PW)
    client = client_for(_device(folder), "Mala#Pass:9", transport=replay)
    try:
        with pytest.raises(DeviceAuthFailed) as exc:
            info = await client.probe()
            await client.list_channels()
            pytest.fail(f"aceptó una contraseña mala: {info}")
        assert not isinstance(exc.value, DeviceLocked)          # contraseña mala ≠ bloqueo
        with pytest.raises(DeviceAuthFailed):
            await client.list_channels()                         # fundido: ni un intento más
    finally:
        await client.aclose()
    assert replay.credentialed == 1 and replay.rejected == 1


@pytest.mark.parametrize("folder", _cases(_has_sdp))
async def test_rtsp_auth_choice_and_single_attempt(folder: Path) -> None:
    meta = _meta(folder)
    async with RtspReplayServer(folder, PW) as srv:
        good = await probe_rtsp("127.0.0.1", srv.port, meta["rtsp_path"], "admin", PW)
        assert good.ok and good.video_codec == meta["main_codec"]
        if srv.challenge is not None:
            offered = parse_challenges(srv.challenge["headers"].get("www-authenticate", []))
            best = max((c for c in offered if c.scheme != "basic" and c.scheme != "unsupported"),
                       key=lambda c: STRENGTH[c.scheme])
            assert good.auth_scheme == best.scheme
        before = srv.credentialed
        bad = await probe_rtsp("127.0.0.1", srv.port, meta["rtsp_path"], "admin", "Mala#Pass:9")
        if srv.challenge is not None:
            assert bad.auth_ok is False and bad.credentialed_requests == 1 and srv.credentialed - before == 1


# --------------------------------------------------------------------------- 6: snapshot
@pytest.mark.parametrize("folder", _cases(lambda f: _has_http(f) and Capability.API_SNAPSHOT in _spec(f).capabilities))
async def test_snapshot_is_jpeg_in_memory(folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    meta = _meta(folder)
    before = set(os.listdir(tmp_path))
    client = client_for(_device(folder), PW, transport=ReplayTransport(folder, PW))
    try:
        await client.probe()
        jpeg = await client.snapshot(meta["channel_ids"][0] if meta["channel_ids"] else 1, "sub")
    finally:
        await client.aclose()
    assert isinstance(jpeg, bytes) and jpeg.startswith(b"\xff\xd8") and jpeg.rstrip(b"\x00").endswith(b"\xff\xd9")
    assert set(os.listdir(tmp_path)) == before


# --------------------------------------------------------------------------- 7: detección
@pytest.mark.parametrize("folder", _cases(lambda f: _spec(f).id != "generic"))
def test_detection_puts_driver_first(folder: Path) -> None:
    meta = _meta(folder)
    hints = DetectionHints.model_validate_json((folder / "discovery" / "hints.json").read_text(encoding="utf-8"))
    ranking = best_match(hints)
    assert ranking, "ningún driver reconoce las pistas"
    top, score = ranking[0]
    assert top.id == meta["driver"], [(s.id, p) for s, p in ranking[:3]]
    if meta["driver"] != "onvif":            # ONVIF genérico es el último recurso: nunca puntúa alto
        assert score > 0.7
