"""Validar un repositorio con el cliente real (`ngclient` dentro de `vms_updater.client`).

Sirve el repositorio por HTTP local (o lo lee como `file://`), parte del `1.root.json` como un equipo recién
instalado, refresca y descarga el descriptor de cada versión publicada y sus componentes. Es lo que hace
`publish --dry-run` y lo que se ejecuta antes de subir nada.
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import json
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tuf.api.metadata import Metadata, Root

from vms_updater.client import TufClient, parse_source
from vms_updater.models import ReleaseDescriptor


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@contextlib.contextmanager
def serve(directory: Path) -> Iterator[str]:
    handler = functools.partial(_Quiet, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


def key_types(root_file: Path) -> dict[str, list[str]]:
    md: Metadata[Root] = Metadata.from_file(str(root_file))
    r = md.signed
    out: dict[str, list[str]] = {}
    for role, rr in r.roles.items():
        out[role] = sorted({r.keys[k].scheme for k in rr.keyids})
    return out


def verify_dir(mode_dir: Path, *, mode: str, trusted_root: bytes | None = None, use_file: bool = False,
               versions: list[str] | None = None) -> dict[str, Any]:
    mode_dir = Path(mode_dir)
    trusted = trusted_root or (mode_dir / "metadata" / "1.root.json").read_bytes()
    report: dict[str, Any] = {"mode": mode, "ok": False, "bundles": {}}
    with tempfile.TemporaryDirectory(prefix="vms-verify-") as tmp:
        ctx: contextlib.AbstractContextManager[str]
        ctx = contextlib.nullcontext(mode_dir.resolve().as_uri() + "/") if use_file else serve(mode_dir)
        with ctx as url:
            client = TufClient(parse_source(url, mode=mode), metadata_dir=Path(tmp) / "m",
                               targets_dir=Path(tmp) / "t", trusted_root=trusted)
            info = client.refresh()
            report["timestamp_expires"] = info.timestamp_expires.isoformat()
            names = sorted(n for n, tf in info.targets.items()
                           if (tf.custom or {}).get("kind") == "bundle")
            for name in names:
                desc = ReleaseDescriptor.model_validate_json(client.read(name))
                if versions and desc.version not in versions:
                    continue
                for ref in desc.components.values():
                    p = client.download(ref.target)
                    if p.stat().st_size != ref.length:
                        raise AssertionError(f"{ref.target}: longitud distinta del descriptor")
                report["bundles"][desc.version] = sorted(desc.components)
            channels = {n: json.loads(client.read(n)) for n, tf in info.targets.items()
                        if (tf.custom or {}).get("kind") == "channel"}
            report["channels"] = {c["channel"]: {"version": c["version"], "paused": c["paused"]}
                                  for c in channels.values()}
    roots = sorted(mode_dir.glob("metadata/*.root.json"), key=lambda p: int(p.name.split(".")[0]))
    report["root_version"] = int(roots[-1].name.split(".")[0]) if roots else 0
    report["key_types"] = key_types(roots[-1]) if roots else {}
    report["ok"] = True
    return report
