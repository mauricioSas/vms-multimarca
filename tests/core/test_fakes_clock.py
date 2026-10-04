"""Las grabaciones simuladas del FakeEngine caen siempre en el «hoy» de la sede (PLAN-V2 §5, fase 0).

Reproduce el fallo de la v1: a las 00:30 CEST (22:30 UTC del día anterior) las grabaciones de «hace
2 h en UTC» quedaban en el día anterior y la línea de tiempo de hoy salía vacía.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from tests.fakes import SITE_TZ, FakeEngine


@pytest.mark.parametrize("now_utc", [
    "2026-10-04T22:30:00+00:00",   # 00:30 CEST del 5-oct (la hora del fallo)
    "2026-10-05T00:15:00+00:00",   # 02:15 CEST
    "2026-10-05T21:59:00+00:00",   # 23:59 CEST
    "2026-10-25T00:30:00+00:00",   # noche del cambio de hora de octubre
    "2026-03-29T00:30:00+00:00",   # noche del cambio de hora de marzo
])
def test_fake_spans_fall_on_the_local_day_of_the_site(now_utc: str) -> None:
    now = datetime.fromisoformat(now_utc)
    engine = FakeEngine(clock=lambda: now)
    spans = asyncio.run(engine.list_recordings("cam-00000001", None, None))
    assert len(spans) == 2
    local_today = now.astimezone(SITE_TZ).date()
    for s in spans:
        assert s.start.tzinfo is not None and s.start.utcoffset() == timezone.utc.utcoffset(None)
        assert s.start.astimezone(SITE_TZ).date() == local_today
        assert s.end.astimezone(SITE_TZ).date() == local_today
