"""Backend simulado para el health check del actualizador.

`deep_health(layout)` devuelve lo que respondería `GET /api/internal/health/deep` en el equipo simulado:
- `VMSBackend` parado → sin respuesta (None);
- la versión con la que arrancó el backend tiene `app/BROKEN` → el backend «cae» (None);
- `app/FEWER_CAMERAS` → graba 2 cámaras menos;
- `VMSEngine` parado → `engine.running = false` y 0 cámaras grabando.
`serve(layout)` lo expone por HTTP (pruebas con subprocesos).
"""
from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from vms_updater.health import DEEP_PATH, HealthResult, evaluate
from vms_updater.layout import Layout

from .vmsctl_double import load_state

CAMERAS = 8


def deep_health(layout: Layout) -> dict[str, Any] | None:
    st = load_state(layout)
    running: dict[str, str] = st["running"]
    ver = running.get("VMSBackend")
    if ver is None:
        return None
    app = layout.version_dir(ver) / "app"
    if (app / "BROKEN").exists():
        return None
    engine = "VMSEngine" in running
    rec = CAMERAS if engine else 0
    if (app / "FEWER_CAMERAS").exists():
        rec = max(0, rec - 2)
    return {"status": "ok" if engine else "degraded", "version": ver, "release": ver,
            "engine": {"running": engine, "api_ok": engine}, "cameras_total": CAMERAS, "cameras_recording": rec,
            "analytics": {"enabled": False}}


class FastHealth:
    """Health check sin esperas (pruebas en proceso)."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.calls: list[str] = []

    def recording_now(self) -> int | None:
        s = deep_health(self.layout)
        return None if s is None else int(s["cameras_recording"])

    def wait_healthy(self, expected_version: str, min_recording: int | None) -> HealthResult:
        self.calls.append(expected_version)
        return evaluate(deep_health(self.layout), expected_version=expected_version, min_recording=min_recording)


@contextlib.contextmanager
def serve(layout: Layout) -> Iterator[str]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            data = deep_health(layout) if self.path == DEEP_PATH else None
            if data is None:
                self.send_response(503)
                self.end_headers()
                return
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
