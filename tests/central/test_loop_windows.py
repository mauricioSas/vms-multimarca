"""Las pruebas del panel central corren en un bucle que psycopg asíncrono acepta (tests/conftest.py).

Regresión en Windows: pytest-asyncio creaba ProactorEventLoop, psycopg lo rechazaba y el grupo de conexiones del
panel acababa en PoolTimeout a los 20 s en casi todas las pruebas de central/. En producción, `python -m central`
ya arranca con SelectorEventLoop (vms/core/aio.py). En Linux y macOS siempre es un bucle selector.
"""
from __future__ import annotations

import psycopg
import pytest

from vms.core import aio


async def test_loop_is_the_one_psycopg_accepts() -> None:
    assert aio.psycopg_async_supported()


@pytest.mark.needs_postgres
async def test_async_psycopg_connects(pg_dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
        cur = await conn.execute("SELECT 1")
        assert await cur.fetchone() == (1,)
