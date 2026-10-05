"""Rutas de B6 para el backend de pruebas de la interfaz (`tests/web/stub_backend.py`).

El backend de pruebas imita la API de la v1 para probar la interfaz sin el backend real. Las páginas de la v2
cargan los módulos de B6, que piden sus rutas al abrirse; sin ellas, el navegador registra «404» en la consola
y las pruebas de `tests/web` (que no admiten errores de consola) fallan.

`StubBackend._build` las monta con `install_ops_routes(app, self)` antes de `mount_web(app)`.

Las respuestas son las de un sistema recién instalado (sin referencias, sin medidas, sin marcadores); el
asistente de primer uso no aparece para no tapar las pruebas de la v1. La lógica real se prueba en `tests/ops`
contra `vms.api` (backend real).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import FastAPI, Request


def install_ops_routes(app: FastAPI, backend: Any) -> None:
    onboarding: dict[str, dict[str, Any]] = {}

    def who(request: Request, level: str = "operator") -> Any:
        return backend._session(request, level)

    @app.get("/api/onboarding/state")
    async def get_onboarding(request: Request) -> dict[str, Any]:
        s = who(request)
        st = onboarding.get(s.username, {"wizard_completed": True, "wizard_step": 0, "tours_seen": [],
                                         "dismissed_hints": []})
        return {**st, "username": s.username, "role": s.role, "has_devices": bool(backend.repo.config.devices),
                "has_cameras": bool(backend.repo.config.cameras), "show_wizard": False}

    @app.put("/api/onboarding/state")
    async def put_onboarding(request: Request) -> dict[str, Any]:
        s = who(request)
        body = await request.json()
        cur = onboarding.setdefault(s.username, {"wizard_completed": True, "wizard_step": 0, "tours_seen": [],
                                                 "dismissed_hints": []})
        cur.update({k: v for k, v in body.items() if k in cur})
        return await get_onboarding(request)

    @app.get("/api/camera-health")
    async def health(request: Request) -> list[dict[str, Any]]:
        who(request)
        return [{"camera_id": c.id, "score": None, "status": "unknown", "causes": ["no_reference"], "since": None,
                 "last_check": None, "references": {}, "clock": None} for c in backend.repo.config.cameras]

    @app.get("/api/clock")
    async def clock(request: Request) -> dict[str, Any]:
        who(request)
        return {"pc": None, "devices": []}

    @app.get("/api/retention-forecast")
    async def forecast(request: Request) -> dict[str, Any]:
        who(request)
        d = await backend.engine.disk_usage()
        return {"at": datetime.now(timezone.utc).isoformat(), "target_days": backend.repo.config.settings.retention.days,
                "forecast_days": 9999.0, "disk_total": d.total, "disk_free": d.free, "reclaimable": 0, "status": "ok",
                "rgpd_warning": False, "cameras": [],
                "message_es": "Todavía no hay grabaciones de las últimas 24 horas para calcular la previsión."}

    @app.get("/api/health-report")
    async def report(request: Request) -> dict[str, Any]:
        who(request)
        return {"site_id": backend.repo.config.settings.site.id, "date": date.today().isoformat(),
                "generated_at": datetime.now(timezone.utc).isoformat(), "status": "ok", "problems": [], "cameras": [],
                "disk": {}, "retention": None, "pc_clock": None}

    @app.get("/api/security-audit/latest")
    async def audit(request: Request) -> None:
        who(request, "admin")
        return None

    @app.get("/api/notifications/settings")
    async def notif(request: Request) -> dict[str, Any]:
        who(request, "admin")
        return {**backend.repo.config.settings.notifications.model_dump(mode="json"), "has_smtp_password": False,
                "has_webhook_secret": False}

    @app.get("/api/notifications/log")
    async def notif_log(request: Request) -> list[Any]:
        who(request, "admin")
        return []

    @app.get("/api/timeline/{camera_id}")
    async def timeline(camera_id: str, request: Request) -> list[Any]:
        who(request)
        return []

    @app.get("/api/bookmarks")
    async def bookmarks(request: Request) -> list[Any]:
        who(request)
        return []

    @app.get("/api/evidence/exports")
    async def exports(request: Request) -> list[Any]:
        who(request)
        return []
