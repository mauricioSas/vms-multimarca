"""Sirve cualquier mock ASGI en un puerto real (127.0.0.1) dentro de un hilo, para pruebas e2e.

    with MockHttpServer(HikvisionMock().app) as srv:
        httpx.get(f"{srv.base_url}/ISAPI/System/deviceInfo", auth=httpx.DigestAuth(...))
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any

import uvicorn


class MockHttpServer:
    def __init__(self, app: Any, host: str = "127.0.0.1", port: int = 0) -> None:
        if port == 0:
            with socket.socket() as s:
                s.bind((host, 0))
                port = s.getsockname()[1]
        self.host, self.port = host, port
        self._server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                                     lifespan="off", access_log=False))
        self._thread = threading.Thread(target=self._server.run, name=f"mock-http-{port}", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 10.0) -> "MockHttpServer":
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("El servidor mock no arrancó a tiempo")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def __enter__(self) -> "MockHttpServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
