"""Agente de latido de sede: `python -m central.agent`.

Cada `VMS_AGENT_INTERVAL_SECONDS` (o `VMS_HEARTBEAT_SECONDS`, 60 s por defecto):

1. Recoge el estado local:
   - `GET /api/health` del backend VMS (sin sesión): estado, versión, motor.
   - Si hay usuario operador configurado (`VMS_AGENT_USERNAME`/`VMS_AGENT_PASSWORD`):
     `GET /api/status` (cámaras con vídeo, disco, analítica) y `GET /api/settings` (nombre de la sede).
   - Del propio PC: nombre del equipo, disco de la carpeta de datos, temperatura (si el sistema
     la expone) y `analytics/status.json` si el backend no lo ha dado.
2. Lo envía al panel central (`POST <VMS_CENTRAL_URL>/api/heartbeat`, `Authorization: Bearer
   <VMS_SITE_TOKEN>`). Reintenta con espera creciente (2, 5, 10 s) dentro del mismo ciclo; si no
   lo consigue, lo deja para el siguiente ciclo. **No acumula latidos**: el latido es un estado,
   no un histórico.

Si el backend no responde, el latido se envía igual con `status: "down"`: así la central sabe
que el PC está encendido pero el servicio de vídeo no.

Opciones: `--once` (un solo envío; código de salida 0/1), `--dry-run` (muestra el latido sin
enviarlo).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import signal
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from vms import __version__
from vms.core.logging_setup import setup_logging
from vms.core.models import Site
from vms.core.paths import AppPaths
from vms.core.rtsp import redact

from .heartbeat import HeartbeatPayload, SiteInfo
from .settings import AgentSettings, load_agent_settings

log = logging.getLogger("central.agent")

RETRY_DELAYS = (2.0, 5.0, 10.0)
DEFAULT_SITE_NAME = str(Site.model_fields["name"].default)
STALE_ANALYTICS_S = 30.0


class AgentConfigError(Exception):
    pass


def _bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def read_temperature() -> float | None:
    """Temperatura máxima de CPU si el sistema la expone (Linux con psutil). None si no hay."""
    try:
        import psutil
    except ImportError:
        return None
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is None:  # Windows y macOS: psutil no la ofrece
        return None
    try:
        data = sensors() or {}
    except Exception as exc:  # algunos kernels dan errores de lectura en sensores concretos
        log.debug("No se pudo leer la temperatura: %s", exc)
        return None
    values = [t.current for entries in data.values() for t in entries if t.current is not None]
    return round(max(values), 1) if values else None


def read_analytics_status(paths: AppPaths) -> dict[str, Any] | None:
    f = paths.analytics_dir / "status.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("analytics/status.json no se puede leer: %s", exc)
        return {"running": False, "stale": True}
    stale = True
    try:
        updated = datetime.fromisoformat(str(data.get("updated_at", "")).replace("Z", "+00:00"))
        stale = (datetime.now(timezone.utc) - updated).total_seconds() > STALE_ANALYTICS_S
    except ValueError:
        pass
    return {"running": bool(data.get("running")) and not stale, "stale": stale}


def local_disk(path: Path) -> dict[str, Any] | None:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        du = shutil.disk_usage(p)
    except OSError as exc:
        log.warning("No se pudo leer el disco de %s: %s", p, exc)
        return None
    return {"percent": round(du.used * 100.0 / du.total, 1) if du.total else 0.0,
            "free_gb": round(du.free / 1e9, 1)}


class HeartbeatAgent:
    def __init__(self, settings: AgentSettings, *, backend_transport: httpx.AsyncBaseTransport | None = None,
                 central_transport: httpx.AsyncBaseTransport | None = None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        if not settings.central_url:
            raise AgentConfigError("Falta VMS_CENTRAL_URL (URL del panel central)")
        if not settings.site_token:
            raise AgentConfigError("Falta VMS_SITE_TOKEN (se crea en la central con «python -m central token "
                                   "create <sede>»)")
        self.settings = settings
        self.paths = AppPaths(settings.base_dir)
        self.retry_delays = retry_delays
        timeout = httpx.Timeout(settings.timeout_seconds)
        self._backend = httpx.AsyncClient(base_url=settings.backend_url, timeout=timeout,
                                          transport=backend_transport, headers={"X-Requested-With": "vms"})
        verify: bool | ssl.SSLContext = settings.verify_tls
        if settings.ca_file:
            verify = ssl.create_default_context(cafile=str(settings.ca_file))
        self._central = httpx.AsyncClient(base_url=settings.central_url, timeout=timeout, verify=verify,
                                          transport=central_transport)
        self._logged_in = False
        self._stop = asyncio.Event()
        self.sent = 0
        self.failures = 0

    async def aclose(self) -> None:
        await self._backend.aclose()
        await self._central.aclose()

    # ------------------------------------------------------------------ recogida
    async def _login(self) -> bool:
        s = self.settings
        if not s.username or not s.password:
            return False
        try:
            r = await self._backend.post("/api/auth/login", json={"username": s.username,
                                                                  "password": s.password.get_secret_value()})
        except httpx.HTTPError as exc:
            log.debug("Login en el backend: %s", exc)
            return False
        if r.status_code != 200:
            log.warning("El agente no pudo iniciar sesión en el backend (HTTP %s); revisa VMS_AGENT_USERNAME",
                        r.status_code)
            return False
        self._logged_in = True
        return True

    async def _get_authed(self, path: str) -> dict[str, Any] | None:
        if not self.settings.username:
            return None
        for attempt in range(2):
            if not self._logged_in and not await self._login():
                return None
            try:
                r = await self._backend.get(path)
            except httpx.HTTPError as exc:
                log.debug("GET %s: %s", path, exc)
                return None
            if r.status_code == 401 and attempt == 0:
                self._logged_in = False
                continue
            if r.status_code != 200:
                log.warning("GET %s respondió HTTP %s", path, r.status_code)
                return None
            data = r.json()
            return data if isinstance(data, dict) else None
        return None

    async def collect(self) -> tuple[SiteInfo, HeartbeatPayload]:
        s = self.settings
        payload: dict[str, Any] = {"hostname": socket.gethostname(), "interval_s": s.interval_seconds,
                                   "agent_version": __version__}
        site = SiteInfo(id=s.site_id)
        backend_ok = False
        try:
            r = await self._backend.get("/api/health")
            if r.status_code == 200:
                h = r.json()
                backend_ok = True
                payload.update({"version": str(h.get("version", ""))[:40],
                                "uptime_s": h.get("uptime_s"),
                                "status": h.get("status", "degraded"),
                                "engine": {"running": bool((h.get("engine") or {}).get("running")),
                                           "api_ok": bool((h.get("engine") or {}).get("api_ok"))}})
            else:
                log.warning("El backend respondió HTTP %s en /api/health", r.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("El backend VMS no responde en %s: %s", s.backend_url, redact(str(exc)))
        if not backend_ok:
            payload.update({"status": "down", "engine": {"running": False}, "backend_reachable": False})
        else:
            payload["backend_reachable"] = True
            status = await self._get_authed("/api/status")
            if status:
                self._merge_status(payload, status)
            cfg = await self._get_authed("/api/settings")
            site_cfg = (cfg or {}).get("site") if isinstance(cfg, dict) else None
            if isinstance(site_cfg, dict):
                if site_cfg.get("id") and site_cfg.get("id") != s.site_id:
                    log.warning("El id de sede del backend (%s) no coincide con VMS_SITE_ID (%s); se usa %s",
                                site_cfg.get("id"), s.site_id, s.site_id)
                name = str(site_cfg.get("name", ""))[:80]
                if name == DEFAULT_SITE_NAME:  # nombre por defecto sin cambiar: no pisa el de la central
                    name = ""
                site = SiteInfo(id=s.site_id, name=name,
                                code=str(site_cfg.get("code", ""))[:32],
                                timezone=str(site_cfg.get("timezone", ""))[:64])
        if "disk" not in payload:
            disk = local_disk(self.paths.recordings_dir)
            if disk:
                payload["disk"] = disk
        if "analytics" not in payload:
            a = read_analytics_status(self.paths)
            if a is not None:
                payload["analytics"] = a
        temp = read_temperature()
        if temp is not None:
            payload["temperature_c"] = temp
        return site, HeartbeatPayload.model_validate(payload)

    @staticmethod
    def _merge_status(payload: dict[str, Any], status: dict[str, Any]) -> None:
        eng = status.get("engine") or {}
        if isinstance(eng, dict):
            payload["engine"] = {"running": bool(eng.get("running")), "restarts": int(eng.get("restarts") or 0),
                                 "api_ok": bool(eng.get("api_ok"))}
        cams_raw = status.get("cameras") or []
        cams = []
        for c in cams_raw if isinstance(cams_raw, list) else []:
            if isinstance(c, dict) and c.get("camera_id"):
                cams.append({"camera_id": c["camera_id"], "name": str(c.get("name") or "")[:80],
                             "online": _bool(c.get("online")), "recording": _bool(c.get("recording"))})
        payload["cameras"] = cams
        payload["cameras_total"] = len(cams)
        payload["cameras_online"] = sum(1 for c in cams if c["online"])
        disk = status.get("disk")
        if isinstance(disk, dict) and disk.get("total"):
            payload["disk"] = {"percent": round(float(disk.get("percent", 0.0)), 1),
                               "free_gb": round(int(disk.get("free", 0)) / 1e9, 1)}
        an = status.get("analytics")
        if isinstance(an, dict):
            payload["analytics"] = {"running": bool(an.get("running")), "stale": bool(an.get("stale", False))}

    # ------------------------------------------------------------------ envío
    async def send(self, site: SiteInfo, payload: HeartbeatPayload) -> bool:
        body = {"site": site.model_dump(), "payload": payload.model_dump(mode="json"),
                "sent_at": datetime.now(timezone.utc).isoformat()}
        headers = {"Authorization": f"Bearer {self.settings.site_token.get_secret_value()}"}  # type: ignore[union-attr]
        attempts = len(self.retry_delays) + 1
        for i in range(attempts):
            try:
                r = await self._central.post("/api/heartbeat", json=body, headers=headers)
            except httpx.HTTPError as exc:
                reason = f"sin conexión con la central ({type(exc).__name__}: {redact(str(exc))})"
            else:
                if r.status_code in (200, 204):
                    self.sent += 1
                    log.debug("Latido enviado (%s)", payload.status)
                    return True
                if r.status_code in (401, 403):
                    log.error("La central rechazó el latido (HTTP %s): revisa VMS_SITE_TOKEN y VMS_SITE_ID",
                              r.status_code)
                    self.failures += 1
                    return False
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    log.error("La central rechazó el latido (HTTP %s): %s", r.status_code, r.text[:300])
                    self.failures += 1
                    return False
                reason = f"HTTP {r.status_code}"
            if i < attempts - 1:
                delay = self.retry_delays[i]
                log.warning("Latido no entregado (%s); reintento en %.0f s", reason, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break  # se pidió parar
                except asyncio.TimeoutError:
                    pass
            else:
                log.warning("Latido no entregado (%s); se intentará en el siguiente ciclo", reason)
        self.failures += 1
        return False

    async def run_once(self) -> bool:
        site, payload = await self.collect()
        return await self.send(site, payload)

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        log.info("Agente de latido de %s → %s cada %s s", self.settings.site_id, self.settings.central_url,
                 self.settings.interval_seconds)
        while not self._stop.is_set():
            started = loop.time()
            try:
                await self.run_once()
            except Exception:  # un fallo inesperado no debe matar el servicio
                log.exception("Error inesperado en el ciclo de latido")
            wait = max(1.0, self.settings.interval_seconds - (loop.time() - started))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
        log.info("Agente de latido detenido")


async def _amain(args: argparse.Namespace) -> int:
    settings = load_agent_settings()
    paths = AppPaths(settings.base_dir)
    setup_logging(paths.logs_dir if not args.dry_run else None, settings.log_level, filename="heartbeat.log")
    if args.dry_run:
        # sin central: se usa una URL ficticia solo para construir el agente
        settings = settings.model_copy(update={"central_url": settings.central_url or "http://127.0.0.1:9",
                                               "site_token": settings.site_token or SecretStr("dry-run")})
    try:
        agent = HeartbeatAgent(settings)
    except AgentConfigError as exc:
        log.error("%s", exc)
        return 2
    try:
        if args.dry_run:
            site, payload = await agent.collect()
            print(json.dumps({"site": site.model_dump(), "payload": payload.model_dump(mode="json")},
                             ensure_ascii=False, indent=2))
            return 0
        if args.once:
            return 0 if await agent.run_once() else 1
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, agent.stop)
            except (NotImplementedError, RuntimeError):  # Windows: se para con Ctrl+C (KeyboardInterrupt)
                pass
        if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
            # el gestor de servicios puede enviar Ctrl+Break al parar el servicio
            signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(agent.stop))
        await agent.run_forever()
        return 0
    finally:
        await agent.aclose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m central.agent", description="Agente de latido de la sede")
    p.add_argument("--once", action="store_true", help="Envía un solo latido y termina")
    p.add_argument("--dry-run", action="store_true", help="Muestra el latido sin enviarlo")
    args = p.parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
