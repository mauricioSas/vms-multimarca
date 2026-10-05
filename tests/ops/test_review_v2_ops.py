"""Regresiones de la revisión cruzada de la v2 (docs/revision-v2/operacion.md): diagnóstico, evidencias, avisos y
ámbito por cámara. (El visor de evidencias está en `test_evidence_viewer.py`, que usa el navegador.) Cada prueba recrea la condición del fallo que encontró el revisor y afirma lo correcto.
"""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.ops.conftest import Harness, api, new_device  # noqa: F401 - fixtures
from tests.ops.test_evidence import _export, _fake_playback
from tests.ops.test_evidence import _setup as _ev_setup
from vms.core.models import Device
from vms.ops import netguard
from vms.ops.diagnose import DiagnoseInput, Diagnoser
from vms.ops.evidence.verify import verify
from vms.ops.notify import WEBHOOK_FAILED, http_post
from vms.vendors import client_for


# --------------------------------------------------------------------------- M3: lo que va al LLM
async def test_llm_never_sees_device_address_or_name() -> None:
    dev = Device(name="Caja Gran Via 32", vendor="hikvision", kind="camera", host="cam-caja.tienda37.local",
                 username="visor")
    seen: dict[str, Any] = {}

    async def rewriter(summary: str, steps: list[Any]) -> str:
        seen["summary"], seen["steps"] = summary, [f"{s.detail_es} {s.action_es}" for s in steps]
        return "Revisa la cámara Caja Gran Via 32 en cam-caja.tienda37.local"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def tcp(h: str, p: int, t: float) -> bool:
        return True

    async def http(h: str, p: int, s: bool, t: float) -> int:
        return 200

    class R:
        status, video_codec, reachable, error = 200, "H.264", True, ""

    async def rtsp(*a: Any, **k: Any) -> Any:
        return R()

    diag = Diagnoser(lambda d, pw: client_for(d, pw, transport=httpx.MockTransport(boom)),
                     tcp_check=tcp, http_probe=http, rtsp_probe=rtsp, rewriter=rewriter)
    res = await diag.run(DiagnoseInput(device=dev, password="Secreta#99", device_id="dev-00000001"))
    sent = " ".join([seen["summary"], *seen["steps"]])
    assert "cam-caja.tienda37.local" not in sent and "Caja Gran Via 32" not in sent
    assert "Secreta#99" not in sent
    # lo que vuelve del LLM también se sanea
    assert res.llm_used and "cam-caja" not in res.summary_es and "Gran Via" not in res.summary_es


# --------------------------------------------------------------------------- Seg M3: zip con nombres repetidos
async def test_evidence_zip_with_duplicate_entry_is_rejected(api: Harness, tmp_path: Path) -> None:  # noqa: F811
    admin, cam, _ = await _ev_setup(api)
    api.state.proxy = _fake_playback([])
    exp = await _export(admin, cam)
    pkg = tmp_path / "orig.zip"
    pkg.write_bytes((await admin.get(exp["download_url"])).content)
    assert verify(pkg).ok
    video = next(n for n in zipfile.ZipFile(pkg).namelist() if n.endswith(".mp4"))
    for variant in (video, video.upper()):
        forged = tmp_path / f"forged-{variant == video}.zip"
        zin = zipfile.ZipFile(pkg)   # writestr(info) cambia los ZipInfo: uno nuevo por variante
        with zipfile.ZipFile(forged, "w") as zout:
            zout.writestr(variant, b"VIDEO FALSO que ve quien abre el zip con el Explorador")
            for info in zin.infolist():
                zout.writestr(info, zin.read(info))
        res = verify(forged)
        assert not res.ok and any("repetidos" in e for e in res.errors), res.errors


# --------------------------------------------------------------------------- Seg M2: SSRF del webhook
@pytest.mark.parametrize("url", [
    "http://receptor.ejemplo.com/hook",              # sin TLS
    "https://127.0.0.1:9997/v3/config/global/patch",  # API de MediaMTX
    "https://localhost/hook", "https://[::1]/hook", "https://169.254.169.254/latest/meta-data/",
    "https://192.168.1.1/", "https://10.0.0.5/", "https://100.64.0.1/", "https://0.0.0.0/",
    "https://[::ffff:127.0.0.1]/", "https://224.0.0.1/", "https://usuario:clave@receptor.ejemplo.com/",
])
def test_webhook_url_must_be_public_https(url: str) -> None:
    with pytest.raises(netguard.UnsafeUrl):
        netguard.check_url(url)


def test_public_https_webhook_is_accepted() -> None:
    assert netguard.check_url("https://hooks.ejemplo.com:8443/vms") == "hooks.ejemplo.com"
    assert netguard.check_url("https://8.8.8.8/hook") == "8.8.8.8"


async def test_dns_name_pointing_to_loopback_is_refused_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(host: str, port: int, **kw: Any) -> list[Any]:
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    sent: list[str] = []
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda *a, **k: sent.append("post"))
    with pytest.raises(netguard.UnsafeUrl):
        await http_post("https://rebind.ejemplo.com/hook", b"{}", {})
    assert sent == []


async def test_webhook_errors_do_not_reveal_whether_the_port_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(url: str) -> None:
        return None

    monkeypatch.setattr(netguard, "resolve_public", ok)

    async def closed(self: Any, url: str, **kw: Any) -> Any:
        raise httpx.ConnectError("refused")

    async def answered(self: Any, url: str, **kw: Any) -> Any:
        return httpx.Response(404)

    errors = []
    for fake in (closed, answered):
        monkeypatch.setattr(httpx.AsyncClient, "post", fake)
        with pytest.raises(RuntimeError) as exc:
            await http_post("https://hooks.ejemplo.com/hook", b"{}", {})
        errors.append(str(exc.value))
    assert errors == [WEBHOOK_FAILED, WEBHOOK_FAILED]


async def test_api_refuses_internal_webhook_urls(api: Harness) -> None:  # noqa: F811
    admin = await api.login()
    cur = (await admin.get("/api/notifications/settings")).json()
    for url in ("http://127.0.0.1:9997/v3/config/global/patch", "https://169.254.169.254/latest/meta-data/"):
        cur.update(webhook_enabled=True, webhook_url=url)
        r = await admin.put("/api/notifications/settings", json=cur)
        assert r.status_code == 422, (url, r.text)


# --------------------------------------------------------------------------- Seg B1: ámbito en el pronóstico
async def test_retention_forecast_only_lists_cameras_in_scope(api: Harness) -> None:  # noqa: F811
    admin = await api.login()
    c1, c2 = (await new_device(admin, import_channels=[1, 2]))["cameras"]
    op = await api.operator()
    r = await admin.patch("/api/users/operador", json={"camera_scope": {"cameras": [c1], "live": True,
                                                                        "playback": True, "export": False}})
    assert r.status_code == 200
    ids = [c["camera_id"] for c in (await op.get("/api/retention-forecast")).json()["cameras"]]
    assert c2 not in ids
    all_ids = [c["camera_id"] for c in (await admin.get("/api/retention-forecast")).json()["cameras"]]
    assert set(all_ids) == {c1, c2}
