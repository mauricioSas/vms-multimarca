"""«Buscar en la red» sin resultados, contra el backend REAL: la pantalla dice por qué y qué hacer.

El caso del PC del usuario: el cable de las cámaras es una «Red no identificada» y Windows la tiene como «Pública»;
antes la búsqueda solo decía «No se encontró ningún equipo». El descubridor de prueba anota en el informe que abre
la API lo mismo que anotaría `discover()` (sin red).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine
from tests.web.conftest import ADMIN, login, make_context, pw_browser  # noqa: F401 (fixtures)
from tests.web.demo import ServerThread
from tests.web.real_backend import RealBackend
from tests.web.stub_backend import StubOptions
from vms.core.interfaces import DiscoveredDevice
from vms.vendors import scanreport

COMMAND = "Set-NetConnectionProfile -InterfaceAlias 'Ethernet' -NetworkCategory Private"


async def _discover_on_public_cable(timeout: float) -> list[DiscoveredDevice]:
    report = scanreport.current()
    assert report is not None, "la API debe abrir el informe de la búsqueda"
    report.nics = [scanreport.NicInfo("192.168.1.80", "Ethernet", "public", "Red no identificada", False, 18)]
    return []


@pytest.fixture
def ui(tmp_path: Path) -> Iterator[ServerThread]:
    backend = RealBackend(tmp_path / "data", FakeEngine(), StubOptions(discover=_discover_on_public_cable))
    server = ServerThread(backend.app).start()
    try:
        yield server
    finally:
        server.stop()


def test_empty_search_explains_the_public_network_with_a_line_to_copy(ui: ServerThread,
                                                                      make_context: Any) -> None:  # noqa: F811
    page = make_context(ui.base_url).new_page()
    login(page, ADMIN)
    page.click("#btn-discover")
    page.click("#btn-discover-run")
    page.wait_for_selector("#discover-report .discover-warn")
    text = page.inner_text("#discover-report")
    assert "«Ethernet» (192.168.1.80, Red no identificada)" in text and "«Pública»" in text and "Qué hacer:" in text
    assert page.inner_text("#discover-report code") == COMMAND
    assert "mira los avisos de arriba" in page.inner_text("#discover-table tbody")
    page.click("#discover-report [data-copy-cmd]")
    page.wait_for_selector(".toast")            # copiada o, si el navegador no deja, cómo copiarla a mano
    # «Buscar cambios de IP» es otra cosa: los avisos de la búsqueda no se quedan
    page.click("#btn-ip-check")
    page.wait_for_function("() => document.querySelector('#discover-report').hidden")
