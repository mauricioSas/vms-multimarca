"""Las directivas del panel central llegan a la tienda (revisión v2, docs/revision-v2/actualizador.md, A4).

Antes: `directive_for` no se llamaba, `POST /api/heartbeat` respondía 204 sin cuerpo, el agente no leía la
respuesta y nadie escribía `updater\\central-directive.json`: «Volver a la anterior», «Comprobar ahora», el
canal o retener desde el panel no hacían nada. Ahora la central la devuelve en la respuesta del latido y el
agente HTTP y el latido directo la entregan igual (`central.directive.deliver`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from central import agent as agent_mod
from central.agent import HeartbeatAgent
from central.heartbeat import HeartbeatSender
from tests.central.conftest import CSRF
from tests.central.test_heartbeat import agent_settings, backend_transport
from vms.core.models import Site

pytestmark = pytest.mark.needs_postgres


async def test_agent_receives_and_delivers_the_panel_directive(tmp_path: Path, central_app: Any,
                                                               admin_client: httpx.AsyncClient,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    token = (await admin_client.post("/api/site-tokens/site-bcn-001", headers=CSRF)).json()["token"]
    delivered: list[tuple[Any, Path]] = []
    monkeypatch.setattr(agent_mod, "deliver_directive", lambda d, p: delivered.append((d, p)) or True)
    agent = HeartbeatAgent(agent_settings(tmp_path, site_token=token), backend_transport=backend_transport(),
                           central_transport=httpx.ASGITransport(app=central_app, client=("100.64.0.7", 1)))
    try:
        assert await agent.run_once() is True
        assert delivered[-1][0] is None                       # el panel no pide nada todavía
        assert delivered[-1][1] == agent.paths.updater_data
        assert (await admin_client.post("/api/updates/sites/site-bcn-001/check", headers=CSRF)).status_code \
            in (200, 202, 204)
        r = await admin_client.post("/api/updates/sites/site-bcn-001/rollback", json={"to": None}, headers=CSRF)
        assert r.status_code in (200, 202, 204), r.text
        assert await agent.run_once() is True
    finally:
        await agent.aclose()
    d = delivered[-1][0]["update"]
    assert d["check"] is True and d["rollback_to"] == "previous"


async def test_direct_heartbeat_delivers_the_same_directive(admin_client: httpx.AsyncClient,
                                                            seeded_dsn: str) -> None:
    r = await admin_client.put("/api/updates/sites/site-bcn-001", json={"channel": "pilot", "hold": True},
                               headers=CSRF)
    assert r.status_code == 200, r.text
    delivered: list[Any] = []

    async def collect() -> dict[str, Any]:
        return {"status": "ok", "cameras": [], "update": {"installed": "2.0.0", "state": "idle",
                                                           "channel": "stable"}}

    sender = HeartbeatSender(seeded_dsn, Site(id="site-bcn-001", name="Barcelona"), 60, collect,
                             deliver=delivered.append)
    assert await sender.send_once() is True
    assert delivered and delivered[-1]["update"]["channel"] == "pilot" and delivered[-1]["update"]["hold"] is True
    with psycopg.connect(seeded_dsn) as c:
        row = c.execute("SELECT installed, reported_channel, channel FROM site_versions "
                        "WHERE site_id = 'site-bcn-001'").fetchone()
    # lo que informa la tienda va a reported_*; lo que pide el panel no cambia
    assert row == ("2.0.0", "stable", "pilot")


async def test_direct_heartbeat_is_saved_even_if_the_directive_cannot_be_read(seeded_dsn: str) -> None:
    calls: list[Any] = []

    async def collect() -> dict[str, Any]:
        return {"status": "ok", "cameras": [], "update": {"installed": "2.0.0"}}

    with psycopg.connect(seeded_dsn, autocommit=True) as c:
        c.execute("ALTER TABLE site_versions RENAME TO site_versions_x")
    try:
        sender = HeartbeatSender(seeded_dsn, Site(id="site-bcn-001", name="Barcelona"), 60, collect,
                                 deliver=calls.append)
        assert await sender.send_once() is True
    finally:
        with psycopg.connect(seeded_dsn, autocommit=True) as c:
            c.execute("ALTER TABLE site_versions_x RENAME TO site_versions")
    assert calls == []          # sin directiva no se borra la que tuviera la tienda
    with psycopg.connect(seeded_dsn) as c:
        seen = c.execute("SELECT last_seen FROM site_heartbeats WHERE site_id = 'site-bcn-001'").fetchone()
    assert seen is not None and (datetime.now(timezone.utc) - seen[0]).total_seconds() < 60


def test_pipe_client_is_the_same_file_in_both_runtimes() -> None:
    root = Path(__file__).resolve().parents[2]
    a = (root / "updater" / "vms_updater" / "pipe_client.py").read_bytes()
    b = (root / "central" / "updater_pipe_client.py").read_bytes()
    assert a == b, "updater/vms_updater/pipe_client.py y central/updater_pipe_client.py tienen que ser iguales"
