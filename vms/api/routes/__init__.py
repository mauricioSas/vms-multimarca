"""Routers de la API REST y de las páginas web.

Fase 0 de la v2: los routers nuevos están registrados (vacíos) para que cada bloque añada sus rutas
sin tocar este archivo ni `app.py` (dueño: arquitecto). Van antes de `pages`.
"""
from __future__ import annotations

from . import (analytics, auth, cameras, counts, devices, diagnostics, events, evidence, health, live, local,
               notifications, onboarding, pages, recordings, security_audit, system, timeline, updates, users,
               vendors, walls)

ROUTERS = [auth.router, users.router, devices.router, cameras.router, walls.router, live.router,
           recordings.router, system.router, counts.router, analytics.router, events.router,
           # v2 (fase 0): B5, B4, B2 y B6
           vendors.router, updates.router, local.router,
           health.router, evidence.router, notifications.router, diagnostics.router, security_audit.router,
           timeline.router, onboarding.router,
           pages.router]

V2_ROUTERS = {"vendors": vendors.router, "updates": updates.router, "local": local.router,
              "health": health.router, "evidence": evidence.router, "notifications": notifications.router,
              "diagnostics": diagnostics.router, "security_audit": security_audit.router,
              "timeline": timeline.router, "onboarding": onboarding.router, "counts": counts.router}

__all__ = ["ROUTERS", "V2_ROUTERS"]
