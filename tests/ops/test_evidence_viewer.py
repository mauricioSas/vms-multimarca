"""`visor.html` del paquete de evidencias en Chromium SIN red (criterio 4 de B6).

Se abre como archivo local (`file://`) en un contexto sin conexión, se le da la carpeta del paquete y
tiene que decir «Todo coincide» (huellas SHA-256 y firma Ed25519 con WebCrypto). Después se cambia UN byte
de un segmento y tiene que detectarlo. La marca de agua va superpuesta (no quemada en el vídeo).
"""
from __future__ import annotations

import asyncio
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.ops.conftest import write_segments
from vms.core.paths import AppPaths
from vms.ops.evidence.export import CameraInfo, EvidenceBuilder, ExportContext
from vms.ops.evidence.keys import load_or_create
from vms.ops.models import EvidenceExport, EvidenceExportRequest

pytestmark = pytest.mark.needs_browser
T0 = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)


def _build(tmp: Path) -> Path:
    paths = AppPaths(tmp / "datos").ensure()
    rec = tmp / "grabaciones"
    write_segments(rec, "cam-00000001", T0, 3, size=50_000)
    req = EvidenceExportRequest(camera_ids=["cam-00000001"], start=T0, end=T0 + timedelta(minutes=2),
                                reason="Hurto en el pasillo 3", case_ref="AT-1", include_mp4=False)
    exp = EvidenceExport(export_id="ev-20261004-abcdef", created_by="ana", request=req)
    ctx = ExportContext(product_version="2.0.0", site={"id": "s0037", "name": "Tienda 37", "code": "037",
                                                       "timezone": "Europe/Madrid"},
                        cameras={"cam-00000001": CameraInfo("cam-00000001", "Pasillo 3", "NVR", "H264")},
                        recordings_dir=rec, protected_dir=tmp / "prot", segment_seconds=60, key=load_or_create(paths))

    async def run() -> Path:
        path, _, _ = await EvidenceBuilder(tmp / "exports").build(exp, ctx)
        return path

    with ThreadPoolExecutor(1) as pool:   # el API síncrono de Playwright ocupa el bucle del hilo principal
        zpath = pool.submit(asyncio.run, run()).result()
    out = tmp / "extraido"
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(out)
    return out / exp.export_id


def _check(browser: Any, folder: Path) -> tuple[str, str, Any]:
    ctx = browser.new_context(offline=True, locale="es-ES")
    page = ctx.new_page()
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    page.goto((folder / "visor.html").as_uri())
    page.set_input_files("#pick", str(folder))
    page.wait_for_function("document.body.dataset.verify && document.body.dataset.verify !== 'info'", timeout=20000)
    state = page.evaluate("document.body.dataset.verify")
    text = page.inner_text("#result")
    wm = page.inner_text("#wm-bar")
    assert all(u.startswith(("file:", "blob:", "data:")) for u in requests), requests   # nada de red
    ctx.close()
    return state, text, wm


def test_viewer_verifies_offline_and_detects_a_changed_byte(browser: Any, tmp_path: Path) -> None:
    folder = _build(tmp_path)
    state, text, wm = _check(browser, folder)
    assert state == "ok", text
    assert "Todo coincide" in text and "firma válida" in text
    assert "ana" in wm and "ev-20261004-abcdef" in wm, "marca de agua con usuario, fecha y paquete"

    seg = next((folder / "video" / "cam-00000001" / "segments").glob("*.mp4"))
    data = bytearray(seg.read_bytes())
    data[1234] ^= 0xFF
    seg.write_bytes(bytes(data))
    state, text, _ = _check(browser, folder)
    assert state == "mismatch" and "no coinciden" in text and "1 de" in text, text
