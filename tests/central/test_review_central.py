"""Regresiones de la revisión de seguridad del panel central.

- X-Forwarded-For: el primer valor lo pone el cliente; detrás de un proxy de confianza se usa el
  último que no sea un proxy.
- Límite de intentos por IP y por usuario, sin «vaciado» del limitador ante un barrido.
- Redirección abierta tras el login con ?next=/\\dominio.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from central.app import _client_ip
from central.security import FailureLimiter
from central.settings import CentralSettings
from tests.central.conftest import ADMIN_PW, CSRF, NOW

ROOT = Path(__file__).resolve().parents[2]


def _req(client: str, xff: list[str]) -> Request:
    headers = [(b"x-forwarded-for", v.encode()) for v in xff]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (client, 1)})


def test_client_ip_uses_rightmost_untrusted_hop() -> None:
    trusted = ["127.0.0.1"]
    assert _client_ip(_req("127.0.0.1", ["1.1.1.1, 9.9.9.9"]), trusted) == "9.9.9.9"   # cliente falsea el 1.º
    assert _client_ip(_req("127.0.0.1", ["9.9.9.9"]), trusted) == "9.9.9.9"
    assert _client_ip(_req("127.0.0.1", ["5.5.5.5", "9.9.9.9, 127.0.0.1"]), trusted) == "9.9.9.9"
    assert _client_ip(_req("8.8.8.8", ["1.1.1.1"]), trusted) == "8.8.8.8"               # sin proxy: se ignora
    assert _client_ip(_req("127.0.0.1", []), trusted) == "127.0.0.1"


@pytest.fixture
async def proxied_app(tmp_path: Path, seeded_dsn: str) -> Any:
    from central import db
    from central.app import create_app

    settings = CentralSettings(_env_file=None, data_dir=tmp_path / "central",  # type: ignore[call-arg]
                               admin_initial_password=ADMIN_PW, trusted_proxies=["testclient", "127.0.0.1"])
    pool = db.make_pool(seeded_dsn, 2)
    await pool.open(wait=True, timeout=20)
    app = create_app(settings, pool=pool, clock=lambda: NOW)
    async with app.router.lifespan_context(app):
        yield app
    await pool.close()


async def test_spoofed_forwarded_for_does_not_bypass_rate_limit(proxied_app: Any) -> None:
    transport = httpx.ASGITransport(app=proxied_app, client=("127.0.0.1", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://central.test") as c:
        codes: dict[int, int] = {}
        for i in range(30):
            r = await c.post("/api/auth/login", json={"username": "admin", "password": f"mala{i}"},
                             headers={**CSRF, "X-Forwarded-For": f"10.0.{i}.1, 203.0.113.7"})
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
    assert codes.get(429, 0) >= 20 and codes.get(401) == 5     # antes: {401: 30}, sin bloqueo


async def test_distributed_attack_on_one_user_is_limited(proxied_app: Any) -> None:
    transport = httpx.ASGITransport(app=proxied_app, client=("127.0.0.1", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://central.test") as c:
        codes = []
        for i in range(35):   # 35 IPs distintas (de verdad, tras el proxy), mismo usuario
            r = await c.post("/api/auth/login", json={"username": "admin", "password": "mala"},
                             headers={**CSRF, "X-Forwarded-For": f"198.51.{i}.9"})
            codes.append(r.status_code)
    assert codes[:30] == [401] * 30 and set(codes[30:]) == {429}


def test_failure_limiter_is_not_reset_by_a_key_flood(monkeypatch: pytest.MonkeyPatch) -> None:
    lim = FailureLimiter(5, 300)
    monkeypatch.setattr(FailureLimiter, "MAX_KEYS", 50)
    for _ in range(5):
        lim.fail("1.2.3.4|admin")
    assert lim.retry_after("1.2.3.4|admin") > 0
    for i in range(200):                       # barrido de claves para «vaciar» el limitador
        lim.fail(f"9.9.9.9|u{i}")
    assert lim.retry_after("1.2.3.4|admin") > 0      # antes: el limitador se vaciaba entero
    assert len(lim._data) <= 52


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_login_next_is_same_origin_only() -> None:
    src = (ROOT / "central" / "web" / "static" / "login.js").read_text(encoding="utf-8").split("(async () =>")[0]
    script = (
        "const fn = new Function('location', process.argv[1] + '; return safeNext;')"
        "({origin: 'http://central.vpn:8700'});"
        "const vals = ['/\\\\evil.example/x', '//evil.example', 'https://evil.example', '/\\t/evil.example',"
        " '/sites?id=1', '/sedes#x', ''];"
        "console.log(JSON.stringify(vals.map(fn)));"
    )
    out = subprocess.run(["node", "-e", script, src], capture_output=True, text=True, timeout=30, check=True)
    assert out.stdout.strip() == '["/","/","/","/","/sites?id=1","/sedes#x","/"]'
