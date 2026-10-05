"""visor.html: la key_id del manifiesto no se comprueba contra la clave pública que firma."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.ops.conftest import browser  # noqa: F401
from tests.ops.test_evidence_viewer import _build, _check
from vms.ops.evidence.verify import verify


def test_visor_accepts_forged_package_with_store_key_id(browser: Any, tmp_path: Path) -> None:  # noqa: F811
    folder = _build(tmp_path)
    mpath = folder / "manifiesto.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    store_kid = m["signing_key"]["key_id"]
    assert _check(browser, folder, key_id=store_kid)[0] == "ok"          # paquete auténtico

    # --- falsificación: vídeo cambiado, manifiesto re-firmado con OTRA clave, key_id de la tienda copiado
    seg = next((folder / "video" / "cam-00000001" / "segments").glob("*.mp4"))
    seg.write_bytes(b"VIDEO MANIPULADO" * 1000)
    forger = Ed25519PrivateKey.generate()
    pub_raw = forger.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pem = forger.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    (folder / "clave-publica.pem").write_bytes(pem)
    m["signing_key"]["public_key"] = base64.b64encode(pub_raw).decode()
    m["signing_key"]["key_id"] = store_kid                              # se deja el de la tienda
    for f in m["files"]:
        p = folder / f["path"]
        data = p.read_bytes()
        f["bytes"], f["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    raw = json.dumps(m, ensure_ascii=False, indent=2).encode("utf-8")
    mpath.write_bytes(raw)
    (folder / "manifiesto.sig").write_text(base64.b64encode(forger.sign(raw)).decode())

    state, text, _ = _check(browser, folder, key_id=store_kid)
    print("visor.html con el key_id de la tienda ->", state, "|", text)
    py = verify(folder, expect_key_id=store_kid)
    print("python -m vms.ops.evidence verify ->", py.ok, py.errors)
    assert state == "ok" and "Todo coincide" in text
    assert not py.ok
