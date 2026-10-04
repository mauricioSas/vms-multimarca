"""Routers de la API REST y de las páginas web."""
from __future__ import annotations

from . import analytics, auth, cameras, devices, events, live, pages, recordings, system, users, walls

ROUTERS = [auth.router, users.router, devices.router, cameras.router, walls.router, live.router,
           recordings.router, system.router, analytics.router, events.router, pages.router]

__all__ = ["ROUTERS"]
