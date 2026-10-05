"""Plugin de pytest OPCIONAL (`pytest -p tests.ops.stub_plugin tests/web`): añade las rutas de B6 al backend de
pruebas de la interfaz sin tocar `tests/web/stub_backend.py`, para comprobar que con la petición de
`tests/ops/web_stub.py` aplicada las pruebas de `tests/web` pasan. No se carga solo."""
from __future__ import annotations

from typing import Any

from tests.ops.web_stub import install_ops_routes


def pytest_configure(config: Any) -> None:
    from tests.web import stub_backend

    original = stub_backend.StubBackend._build
    if getattr(original, "_b6", False):
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
