"""Panel central contra una PostgreSQL real (pgserver) con datos sembrados."""
from __future__ import annotations

from collections.abc import Callable

import httpx
import psycopg
import pytest

from tests.central.conftest import ADMIN_PW, CSRF, login

pytestmark = pytest.mark.needs_postgres


# =========================================================================== autenticación
async def test_requires_login(client_factory: Callable[..., httpx.AsyncClient]) -> None:
    c = client_factory()
    r = await c.get("/api/sites")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    r = await c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_login_wrong_password_and_rate_limit(client_factory: Callable[..., httpx.AsyncClient]) -> None:
    c = client_factory("10.1.1.1")
    for _ in range(5):
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "mala-clave"}, headers=CSRF)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"
    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW}, headers=CSRF)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    # otra IP no está bloqueada
    other = client_factory("10.1.1.2")
    await login(other)


async def test_csrf_header_required(client_factory: Callable[..., httpx.AsyncClient]) -> None:
    c = client_factory()
    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf"


async def test_me_logout_and_cookie_flags(client_factory: Callable[..., httpx.AsyncClient]) -> None:
    c = client_factory()
    r = await c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW}, headers=CSRF)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert (await c.get("/api/auth/me")).json() == {"username": "admin", "role": "admin"}
    assert (await c.post("/api/auth/logout", headers=CSRF)).status_code == 204
    assert (await c.get("/api/auth/me")).status_code == 401


async def test_setup_only_without_users_and_from_localhost(central_settings, seeded_dsn) -> None:  # type: ignore[no-untyped-def]
    from central import db
    from central.app import create_app

    settings = central_settings.model_copy(update={"admin_initial_password": None})
    pool = db.make_pool(seeded_dsn, 2)
    await pool.open(wait=True)
    app = create_app(settings, pool=pool)
    try:
        async with app.router.lifespan_context(app):
            remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.168.1.50", 1)),
                                       base_url="http://central.test")
            local = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                                      base_url="http://central.test")
            async with remote, local:
                assert (await local.get("/api/auth/setup")).json() == {"needed": True}
                assert (await local.get("/")).headers["location"] == "/setup"
                body = {"username": "jefa", "password": "clave-segura-1"}
                assert (await remote.post("/api/auth/setup", json=body, headers=CSRF)).status_code == 403
                short = await local.post("/api/auth/setup", json={"username": "jefa", "password": "corta"},
                                         headers=CSRF)
                assert short.status_code == 422
                assert (await local.post("/api/auth/setup", json=body, headers=CSRF)).status_code == 201
                assert (await local.post("/api/auth/setup", json=body, headers=CSRF)).status_code == 409
                await login(local, "jefa", "clave-segura-1")
    finally:
        await pool.close()


# =========================================================================== usuarios
async def test_users_admin_rules(admin_client: httpx.AsyncClient,
                                 client_factory: Callable[..., httpx.AsyncClient]) -> None:
    r = await admin_client.post("/api/users", json={"username": "ope", "password": "operador-123",
                                                    "role": "operator"}, headers=CSRF)
    assert r.status_code == 201 and r.json()["role"] == "operator"
    assert "password_hash" not in r.json()
    assert (await admin_client.post("/api/users", json={"username": "OPE", "password": "operador-123"},
                                    headers=CSRF)).status_code == 409
    # no se puede dejar el sistema sin administrador habilitado
    r = await admin_client.patch("/api/users/admin", json={"role": "operator"}, headers=CSRF)
    assert r.status_code == 409
    r = await admin_client.delete("/api/users/admin", headers=CSRF)
    assert r.status_code == 409
    # el operador ve sedes pero no administra
    ope = client_factory()
    await login(ope, "ope", "operador-123")
    assert (await ope.get("/api/sites")).status_code == 200
    assert (await ope.get("/api/users")).status_code == 403
    assert (await ope.post("/api/site-tokens/site-bcn-001", headers=CSRF)).status_code == 403
    # desactivarlo cierra su sesión
    assert (await admin_client.patch("/api/users/ope", json={"enabled": False}, headers=CSRF)).status_code == 200
    assert (await ope.get("/api/sites")).status_code == 401
    assert (await admin_client.delete("/api/users/ope", headers=CSRF)).status_code == 204


# =========================================================================== sedes y conteos
async def test_sites_overview_status_and_counts(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.get("/api/sites")
    assert r.status_code == 200
    data = r.json()
    assert data["generated_at"] == "2026-10-07T10:30:00Z"
    by_id = {s["site_id"]: s for s in data["sites"]}
    assert set(by_id) == {"site-bcn-001", "site-mad-002", "site-can-003"}  # la inactiva no sale

    bcn = by_id["site-bcn-001"]
    assert bcn["online"] is True and bcn["state"] == "degraded" and bcn["age_s"] == 30.0
    assert bcn["today"] == {"in": 13, "out": 4, "alerts": 2}           # 3 + 10; no cuenta el futuro
    assert bcn["week"]["in"] == 18 and bcn["week"]["prev_in"] == 9    # +5 de ayer; semana anterior hasta el mismo tramo
    assert bcn["alerts_open"] == 1
    assert bcn["cameras_total"] == 2 and bcn["cameras_online"] == 1 and bcn["disk_percent"] == 71.5
    assert bcn["day_start"] == "2026-10-06T22:00:00Z"                 # medianoche de Madrid
    assert bcn["week_start"] == "2026-10-04T22:00:00Z"                # lunes de Madrid
    assert bcn["last_report_week"] == "2026-09-28"

    mad = by_id["site-mad-002"]
    assert mad["online"] is False and mad["state"] == "down"          # 10 min sin latido > 3 × 60 s
    can = by_id["site-can-003"]
    assert can["state"] == "unknown" and can["last_seen"] is None
    assert can["day_start"] == "2026-10-06T23:00:00Z"                 # medianoche de Canarias
    assert can["today"]["in"] == 2                                    # las 23:30 de Canarias son ayer


async def test_counts_hourly_buckets_in_site_timezone(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.get("/api/sites/site-bcn-001/counts?range=today")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["bucket"] == "hour" and d["timezone"] == "Europe/Madrid"
    assert d["from"] == "2026-10-06T22:00:00Z" and d["to"] == "2026-10-07T10:30:00Z"
    assert len(d["series"]) == 13                                      # 00:00 … 12:00 en Madrid
    assert d["series"][0] == {"start": "2026-10-06T22:00:00Z", "in": 3, "out": 0}
    assert d["series"][10] == {"start": "2026-10-07T08:00:00Z", "in": 10, "out": 4}
    assert d["total"] == {"in": 13, "out": 4}

    week = (await admin_client.get("/api/sites/site-bcn-001/counts?range=week")).json()
    assert week["bucket"] == "day" and len(week["series"]) == 3        # lunes, martes, miércoles
    assert [p["in"] for p in week["series"]] == [0, 5, 13]

    custom = await admin_client.get("/api/sites/site-bcn-001/counts",
                                    params={"from": "2026-09-29T00:00:00Z", "to": "2026-09-30T00:00:00Z",
                                            "bucket": "day"})
    assert custom.json()["total"]["in"] == 9

    bad = await admin_client.get("/api/sites/site-bcn-001/counts?range=siempre")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
    too_long = await admin_client.get("/api/sites/site-bcn-001/counts",
                                      params={"from": "2026-01-01T00:00:00Z", "to": "2026-10-01T00:00:00Z",
                                              "bucket": "hour"})
    assert too_long.status_code == 422
    assert (await admin_client.get("/api/sites/no-existe/counts")).status_code == 404
    assert (await admin_client.get("/api/sites/..%2F..%2Fetc/counts")).status_code == 404


async def test_occupancy_alerts_and_detail(admin_client: httpx.AsyncClient) -> None:
    occ = (await admin_client.get("/api/sites/site-bcn-001/occupancy?range=today")).json()
    hour10 = next(p for p in occ["series"] if p["start"] == "2026-10-07T08:00:00Z")
    assert hour10 == {"start": "2026-10-07T08:00:00Z", "avg_people": 4.0, "max_people": 6,
                      "seconds_over_threshold": 80, "minutes": 2}

    alerts = (await admin_client.get("/api/sites/site-bcn-001/queue-alerts?range=today")).json()["alerts"]
    assert len(alerts) == 2
    open_alert, closed = alerts
    assert open_alert["ended_at"] is None and open_alert["duration_s"] == 1800.0 and open_alert["notify_failed"]
    assert closed["duration_s"] == 300.0 and closed["rule_name"] == "Cola cajas" and closed["camera_name"] == "Cajas"

    detail = (await admin_client.get("/api/sites/site-bcn-001")).json()
    cams = {c["camera_id"]: c for c in detail["cameras"]}
    assert cams["cam-cash0001"]["online"] is False and cams["cam-door0001"]["recording"] is True
    assert {r["rule_id"] for r in detail["rules"]} == {"rule-door01", "rule-queue1"}
    # una sede caída no muestra el estado de cámaras viejo como si fuera actual
    mad = (await admin_client.get("/api/sites/site-mad-002")).json()
    assert all(c["online"] is None for c in mad["cameras"])


async def test_compare_and_top_queues(admin_client: httpx.AsyncClient) -> None:
    cmp = (await admin_client.get("/api/compare?range=last7")).json()
    assert cmp["from"] == "2026-09-30T22:00:00Z"                       # 7 días locales incluido hoy
    rows = {s["site_id"]: s for s in cmp["sites"]}
    assert rows["site-bcn-001"]["entries"] == 18 and rows["site-mad-002"]["entries"] == 20
    assert cmp["sites"][0]["site_id"] == "site-mad-002"               # ordenado por entradas
    assert rows["site-bcn-001"]["alerts"] == 2 and rows["site-bcn-001"]["alert_seconds"] == 2100.0
    assert rows["site-bcn-001"]["avg_people"] == 4.0 and rows["site-bcn-001"]["max_people"] == 6
    assert "site-old-004" not in rows

    top = (await admin_client.get("/api/queues/top?range=last7&limit=5")).json()["queues"]
    assert [(q["site_id"], q["rule_id"], q["alerts"]) for q in top] == [
        ("site-bcn-001", "rule-queue1", 2), ("site-mad-002", "rule-queue2", 1)]
    assert top[0]["peak_people"] == 9 and top[0]["rule_name"] == "Cola cajas"
    assert (await admin_client.get("/api/compare?tz=Marte/Olympus")).status_code == 422


async def test_reports(admin_client: httpx.AsyncClient) -> None:
    rows = (await admin_client.get("/api/sites/site-bcn-001/reports")).json()
    assert [r["week_start"] for r in rows] == ["2026-09-28", "2026-09-21"]
    assert rows[1]["has_error"] is True
    rep = (await admin_client.get("/api/sites/site-bcn-001/reports/2026-09-28")).json()
    assert rep["metrics"] == {"entries_total": 1234} and rep["body_markdown"].startswith("## Resumen")
    assert rep["site_name"] == "Tienda Gràcia"
    assert (await admin_client.get("/api/sites/site-bcn-001/reports/2026-09-14")).status_code == 404
    assert (await admin_client.get("/api/sites/site-bcn-001/reports/no-es-fecha")).status_code == 422
    latest = (await admin_client.get("/api/reports/latest")).json()
    assert latest == [{"site_id": "site-bcn-001", "site_name": "Tienda Gràcia", "week_start": "2026-09-28",
                       "generated_at": latest[0]["generated_at"], "status": "ok", "provider": "anthropic",
                       "model": "modelo-x"}]


# =========================================================================== latido HTTP
async def _heartbeat(client: httpx.AsyncClient, token: str, site_id: str, **payload: object) -> httpx.Response:
    body = {"site": {"id": site_id, "name": "Tienda Nueva", "code": "N1", "timezone": "Europe/Madrid"},
            "payload": {"version": "0.1.0", "hostname": "PC-NUEVA", "status": "ok",
                        "cameras": [{"camera_id": "cam-aaaa0001", "name": "Puerta", "online": True,
                                     "recording": True}],
                        "cameras_total": 1, "cameras_online": 1, **payload}}
    return await client.post("/api/heartbeat", json=body, headers={"Authorization": f"Bearer {token}"})


async def test_heartbeat_endpoint(admin_client: httpx.AsyncClient, client_factory: Callable[..., httpx.AsyncClient],
                                  seeded_dsn: str) -> None:
    r = await admin_client.post("/api/site-tokens/site-new-005", headers=CSRF)
    assert r.status_code == 201
    token = r.json()["token"]
    other = (await admin_client.post("/api/site-tokens/site-bcn-001", headers=CSRF)).json()["token"]
    listing = (await admin_client.get("/api/site-tokens")).json()
    assert {t["site_id"] for t in listing} == {"site-new-005", "site-bcn-001"}
    assert all("token" not in t and "sha256" not in t for t in listing)

    store = client_factory("100.64.0.5")  # la sede no usa cookie ni CSRF: usa su token
    r = await _heartbeat(store, token, "site-new-005", temperature_c=48.5)
    assert r.status_code == 200 and r.json() == {"directive": None}   # el panel no pide nada a esta sede
    with psycopg.connect(seeded_dsn) as c:
        site = c.execute("SELECT name, code, timezone FROM sites WHERE site_id='site-new-005'").fetchone()
        hb = c.execute("SELECT hostname, status, payload->>'temperature_c', now() - last_seen < interval '1 min' "
                       "FROM site_heartbeats WHERE site_id='site-new-005'").fetchone()
        cams = c.execute("SELECT camera_id, name FROM site_cameras WHERE site_id='site-new-005'").fetchall()
    assert site == ("Tienda Nueva", "N1", "Europe/Madrid")
    assert hb == ("PC-NUEVA", "ok", "48.5", True)
    assert cams == [("cam-aaaa0001", "Puerta")]

    # token de otra sede → 403; token falso → 401; sin token → 401
    assert (await _heartbeat(store, other, "site-new-005")).status_code == 403
    bad = await _heartbeat(store, "vms_falso", "site-new-005")
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_token"
    assert (await store.post("/api/heartbeat", json={})).status_code == 401
    # formato no válido
    r = await store.post("/api/heartbeat", content=b"{no json", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422
    r = await _heartbeat(store, token, "site-new-005", cameras=[{"camera_id": "MAL ID"}])
    assert r.status_code == 422
    # demasiado grande
    big = await store.post("/api/heartbeat", content=b"x" * (200 * 1024),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert big.status_code == 413
    # revocado → deja de valer
    assert (await admin_client.delete("/api/site-tokens/site-new-005", headers=CSRF)).status_code == 204
    assert (await _heartbeat(store, token, "site-new-005")).status_code == 401


async def test_heartbeat_bad_token_rate_limited(admin_client: httpx.AsyncClient,
                                                client_factory: Callable[..., httpx.AsyncClient]) -> None:
    attacker = client_factory("203.0.113.9")
    for _ in range(20):
        assert (await _heartbeat(attacker, "vms_adivina", "site-bcn-001")).status_code == 401
    assert (await _heartbeat(attacker, "vms_adivina", "site-bcn-001")).status_code == 429


async def test_health_and_pages(client_factory: Callable[..., httpx.AsyncClient],
                                admin_client: httpx.AsyncClient) -> None:
    anon = client_factory()
    h = (await anon.get("/api/health")).json()
    assert h["status"] == "ok" and h["db"] == "ok"
    login_page = await anon.get("/login")
    assert login_page.status_code == 200 and "Panel central" in login_page.text
    assert "default-src 'self'" in login_page.headers["content-security-policy"]
    for path in ("/", "/sites/site-bcn-001", "/sites/site-bcn-001/reports/2026-09-28", "/admin"):
        r = await admin_client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), path
    js = await anon.get("/static/common.js")
    assert js.status_code == 200 and "X-Requested-With" in js.text


async def test_db_down_gives_clear_error(central_settings) -> None:  # type: ignore[no-untyped-def]
    from central import db
    from central.app import create_app
    from tests.conftest import get_free_port

    pool = db.make_pool(f"postgresql://nadie@127.0.0.1:{get_free_port()}/nada", 1)
    await pool.open(wait=False)
    app = create_app(central_settings, pool=pool)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
                await login(c)
                h = (await c.get("/api/health")).json()
                assert h["status"] == "degraded" and h["db"] == "error"
                r = await c.get("/api/sites")
                assert r.status_code == 503 and r.json()["error"]["code"] == "db_unavailable"
    finally:
        await pool.close()
