"""Piezas de seguridad del panel central (sin base de datos)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from central.app import site_state
from central.security import FailureLimiter, SessionStore, SiteTokenStore, hash_password, verify_password
from central.settings import AgentSettings, CentralSettings


def test_password_hashing() -> None:
    h = hash_password("clave-segura-1")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "clave-segura-1")
    assert not verify_password(h, "otra")
    assert not verify_password(None, "clave-segura-1")
    assert not verify_password("no-es-un-hash", "x")


def test_sessions_expire_and_follow_user() -> None:
    store = SessionStore(hours=1)
    s = store.create("ana", "operator")
    assert store.get(s.token) is s
    store.update_role("ANA", "admin")
    assert s.role == "admin"
    s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert store.get(s.token) is None
    s2 = store.create("ana", "operator")
    store.delete_user("Ana")
    assert store.get(s2.token) is None
    assert store.get(None) is None and store.get("") is None


def test_failure_limiter_window() -> None:
    lim = FailureLimiter(2, 0.2)
    assert lim.retry_after("k") == 0
    lim.fail("k")
    lim.fail("k")
    assert lim.retry_after("k") >= 1
    time.sleep(0.25)
    assert lim.retry_after("k") == 0
    lim.fail("k")
    lim.reset("k")
    assert lim.retry_after("k") == 0


def test_site_tokens_hashed_and_reloaded(tmp_path: Path) -> None:
    f = tmp_path / "site_tokens.json"
    store = SiteTokenStore(f)
    token = store.issue("site-bcn-001")
    assert token.startswith("vms_") and len(token) > 40
    assert token not in f.read_text(encoding="utf-8")          # solo se guarda el hash
    assert store.verify(token) == "site-bcn-001"
    assert store.verify(token + "x") is None and store.verify(None) is None
    # rotar invalida el anterior
    token2 = store.issue("site-bcn-001")
    assert store.verify(token) is None and store.verify(token2) == "site-bcn-001"
    # otro proceso (la CLI) crea un token: el panel lo ve sin reiniciar
    time.sleep(0.01)
    token3 = SiteTokenStore(f).issue("site-mad-002")
    assert store.verify(token3) == "site-mad-002"
    assert [r["site_id"] for r in store.list()] == ["site-bcn-001", "site-mad-002"]
    assert store.revoke("site-bcn-001") and not store.revoke("site-bcn-001")
    assert store.verify(token2) is None


def test_site_tokens_corrupt_file_keeps_loaded(tmp_path: Path) -> None:
    f = tmp_path / "site_tokens.json"
    store = SiteTokenStore(f)
    token = store.issue("site-bcn-001")
    time.sleep(0.01)
    f.write_text("{roto", encoding="utf-8")
    assert store.verify(token) == "site-bcn-001"
    f.write_text(json.dumps({"sites": {"MAL ID": {"sha256": "x"}}}), encoding="utf-8")
    fresh = SiteTokenStore(f)
    assert fresh.list() == []


def test_site_state_rules() -> None:
    now = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    row = {"last_seen": now - timedelta(seconds=170), "reported_status": "ok", "payload": {"interval_s": 60}}
    assert site_state(row, now, 60, 3)["state"] == "ok"
    row["last_seen"] = now - timedelta(seconds=181)
    assert site_state(row, now, 60, 3) == {"online": False, "state": "down", "age_s": 181.0, "interval_s": 60}
    row["payload"] = {"interval_s": 300}   # una sede con latido cada 5 min tarda más en darse por caída
    assert site_state(row, now, 60, 3)["online"] is True
    assert site_state({"last_seen": None}, now, 60, 3)["state"] == "unknown"
    row = {"last_seen": now, "reported_status": "degraded", "payload": {"interval_s": "basura"}}
    assert site_state(row, now, 60, 3) == {"online": True, "state": "degraded", "age_s": 0.0, "interval_s": 60}


def test_settings_share_common_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VMS_PG_DSN", "postgresql://u:secreta@h/db")
    monkeypatch.setenv("VMS_HEARTBEAT_SECONDS", "30")
    monkeypatch.setenv("VMS_SITE_ID", "site-bcn-001")
    monkeypatch.setenv("VMS_CENTRAL_URL", "https://central.vpn:8700/")
    c = CentralSettings(_env_file=None)  # type: ignore[call-arg]
    assert c.pg_dsn is not None and "secreta" not in repr(c)
    assert c.heartbeat_seconds == 30 and c.http_port == 8700
    a = AgentSettings(_env_file=None)  # type: ignore[call-arg]
    assert a.central_url == "https://central.vpn:8700" and a.site_id == "site-bcn-001" and a.interval_seconds == 30
    monkeypatch.setenv("VMS_CENTRAL_PG_DSN", "postgresql://otro@h/db")
    assert CentralSettings(_env_file=None).pg_dsn.get_secret_value() == "postgresql://otro@h/db"  # type: ignore[call-arg,union-attr]
