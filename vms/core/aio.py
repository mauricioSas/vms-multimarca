"""Bucle de eventos compatible con psycopg en Windows.

En Windows, `asyncio.run()` y uvicorn usan por defecto `ProactorEventLoop`, y psycopg 3 se
niega a trabajar en modo asíncrono con ese bucle («Psycopg cannot use the 'ProactorEventLoop'
to run in async mode»). Todos los procesos que usan `psycopg.AsyncConnection` (analítica,
informe semanal y panel central) arrancan con `SelectorEventLoop` en Windows a través de estas
funciones. En macOS y Linux no cambia nada (ya es un bucle selector).

El backend VMS es la excepción: necesita Proactor para lanzar MediaMTX como subproceso, así que
no usa psycopg asíncrono (el latido directo escribe con psycopg síncrono en un hilo).
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

UVICORN_AUTO_LOOP = "auto"
UVICORN_SELECTOR_LOOP = "asyncio:SelectorEventLoop"


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Factoría de bucle para `asyncio.run(..., loop_factory=...)`. None = la de por defecto."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def run(main: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` con un bucle que psycopg acepta también en Windows."""
    return asyncio.run(main, loop_factory=loop_factory())


def uvicorn_loop() -> str:
    """Valor del parámetro `loop` de uvicorn para servidores que usan psycopg asíncrono."""
    return UVICORN_SELECTOR_LOOP if sys.platform == "win32" else UVICORN_AUTO_LOOP


def psycopg_async_supported(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """False si psycopg rechazará el bucle actual (Proactor en Windows)."""
    if sys.platform != "win32":
        return True
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    current = loop or asyncio.get_running_loop()
    return proactor is None or not isinstance(current, proactor)
