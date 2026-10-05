"""Rutas de B6 en el backend de pruebas de la interfaz (`tests/web/stub_backend.py`) sin tocar ese archivo.

Mientras el dueño de `tests/web/stub_backend.py` no aplique la petición de `tests/ops/web_stub.py` (llamar a
`install_ops_routes(app, self)` antes de `mount_web(app)`), las páginas de la v2 piden rutas de B6 que el
backend de pruebas no tiene y `tests/web/test_ui.py` falla por los «404» de la consola.

- `tests/ops/conftest.py` llama a `install()` al cargarse: con `pytest` (la suite completa, que siempre
  recoge `tests/ops`) el backend de pruebas ya trae las rutas.
- Para correr solo `tests/web`: `pytest -p tests.ops.stub_plugin tests/web`.

Cuando la petición esté aplicada, `install()` no hace nada (detecta que `StubBackend` ya las monta) y este
archivo se puede borrar.
"""
from __future__ import annotations

import inspect
from typing import Any

from tests.ops.web_stub import install_ops_routes


def _already_native(stub_backend: Any) -> bool:
    try:
        return "install_ops_routes" in inspect.getsource(stub_backend.StubBackend._build)
    except (OSError, TypeError):
        return False


def install() -> None:
    from tests.web import stub_backend

    original = stub_backend.StubBackend._build
    if getattr(original, "_b6", False) or _already_native(stub_backend):
        return

    def build(self: Any) -> Any:
        import vms.web
        real_mount = vms.web.mount_web

        def mount(app: Any) -> None:
            install_ops_routes(app, self)
            real_mount(app)
        stub_backend.mount_web = mount  # type: ignore[assignment]
        try:
            return original(self)
        finally:
            stub_backend.mount_web = real_mount  # type: ignore[assignment]

    build._b6 = True  # type: ignore[attr-defined]
    stub_backend.StubBackend._build = build  # type: ignore[method-assign]


def pytest_configure(config: Any) -> None:
    install()
