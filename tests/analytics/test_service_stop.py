"""Parada ordenada de `VMSAnalytics` bajo `vmsctl run` (CONTRATO §14.1).

En Windows, la única orden de parada que manda vmsctl es cerrar la entrada estándar (`VMS_STOP_ON_STDIN_EOF=1`):
ni SIGTERM ni Ctrl+Break, aunque el comentario del código decía que el servicio enviaba Ctrl+Break. La analítica
solo atendía señales, así que vmsctl la mataba a los 10 s en cada parada.
"""
from __future__ import annotations

import asyncio
import io
import sys

import pytest

from analytics import __main__ as amain


class ClosedStdin:
    buffer = io.BytesIO(b"")         # fin de la entrada nada más empezar: vmsctl ya la cerró


async def test_closing_stdin_stops_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", ClosedStdin())
    monkeypatch.setattr(amain.signal, "signal", lambda *_a: None)      # no tocar las señales de pytest
    monkeypatch.setenv("VMS_STOP_ON_STDIN_EOF", "1")
    stop = asyncio.Event()
    amain._install_signal_handlers(asyncio.get_running_loop(), stop)
    await asyncio.wait_for(stop.wait(), 5)


async def test_in_a_console_closing_stdin_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", ClosedStdin())
    monkeypatch.setattr(amain.signal, "signal", lambda *_a: None)
    monkeypatch.delenv("VMS_STOP_ON_STDIN_EOF", raising=False)
    stop = asyncio.Event()
    amain._install_signal_handlers(asyncio.get_running_loop(), stop)
    await asyncio.sleep(0.3)
    assert not stop.is_set()
