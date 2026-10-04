"""Prueba de sistema de punta a punta, con procesos reales (no forma parte de `pytest`).

    .venv/bin/python -m tests.e2e.system_check [--record-seconds 150] [--load-seconds 300]

Arranca TODO como en una sede real, cada pieza en su propio proceso:
  simulador de cámaras (2 NVR estilo Hikvision y Dahua con su API HTTP simulada + cámara de puerta
  con vídeo real de personas) → backend `python -m vms` (con su MediaMTX) → analítica
  `python -m analytics` (RF-DETR nano real) → PostgreSQL embebido (pgserver) → panel central
  `python -m central` y agente de latido `python -m central.agent`, y Chromium (Playwright) como los
  4 monitores en modo kiosco.

Pasos (los de tests/e2e/RESULTADOS.md): a) simulador · b) alta por API y muros · c) vídeo WebRTC en
los 4 muros · d) grabación, línea de tiempo, MP4 válido y reproducción en la interfaz · e) corte de
20 s de una cámara · f) MediaMTX matado · g) analítica, PostgreSQL, Telegram simulado e informe
semanal · h) panel central · i) carga 16 cámaras en un muro 4x4.

Resultados en `.tmp/e2e-system/results.json`, capturas en `tests/e2e/screenshots/sistema/` y el
anexo `tests/e2e/RESULTADOS-datos.md` generado con los valores medidos.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import psutil

from vms.core.mtx_auth import MtxCredentials
from tools.camsim.simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice
from tools.dev_run import (HEADERS, ROOT, BackendClient, EmbeddedPostgres, Service, base_env, clean_exit,
                           free_port, python_cmd, start_simulator, wait_until, write_env_file)
from tools.mocks.hikvision import HikChannel, HikvisionMock
from tools.mocks.server import MockHttpServer

log = logging.getLogger("system_check")

WORK = ROOT / ".tmp" / "e2e-system"
SHOTS = ROOT / "tests" / "e2e" / "screenshots" / "sistema"
SITE_ID = "site-e2e-001"
TG_TOKEN = "123456:TEST-token-de-pruebas"
TG_CHAT = "-1009876543210"
MADRID = ZoneInfo("Europe/Madrid")
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

PC_TRACKER = """
(() => {
  const Orig = window.RTCPeerConnection;
  if (!Orig || window.__pcTracker) return;
  const live = new Set();
  window.__pcTracker = { created: 0, get open() { return [...live].filter(p => p.connectionState !== 'closed').length; } };
  window.RTCPeerConnection = function (...args) {
    const pc = new Orig(...args); window.__pcTracker.created++; live.add(pc);
    const close = pc.close.bind(pc); pc.close = () => { live.delete(pc); return close(); }; return pc;
  };
  window.RTCPeerConnection.prototype = Orig.prototype;
})();
"""

CELLS_JS = """() => {
  const meta = new Map(window.__vmsWall.cells().map(c => [String(c.index), c]));
  return [...document.querySelectorAll('.cell')].filter(c => meta.get(c.dataset.index)?.cameraId).map(c => {
    const v = c.querySelector('video'); const m = meta.get(c.dataset.index);
    const q = v && v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
    return {index: Number(c.dataset.index), camera: m.cameraId, stream: m.stream, state: c.dataset.state,
            w: v ? v.videoWidth : 0, h: v ? v.videoHeight : 0, t: v ? v.currentTime : 0,
            frames: q ? q.totalVideoFrames : 0, dropped: q ? q.droppedVideoFrames : 0,
            reconnects: m.reader ? m.reader.reconnects : 0};
  });
}"""


# =========================================================================== resultados
@dataclass
class Step:
    key: str
    title: str
    ok: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0


class Results:
    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.started = datetime.now(timezone.utc)

    def step(self, key: str, title: str) -> Step:
        s = Step(key, title)
        self.steps.append(s)
        log.info("===== %s) %s", key, title)
        return s

    def save(self, path: Path) -> None:
        data = {"started_at": self.started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                "platform": f"{sys.platform} {os.uname().machine if hasattr(os, 'uname') else ''}",
                "steps": [s.__dict__ for s in self.steps]}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


# =========================================================================== Telegram simulado
class TelegramMock:
    """Bot API mínima: guarda cada sendMessage (sin imágenes, solo texto)."""

    def __init__(self) -> None:
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        self.messages: list[dict[str, Any]] = []

        async def send(request: Request) -> JSONResponse:
            body = await request.json()
            self.messages.append({"token_ok": request.path_params["token"] == TG_TOKEN, **body,
                                  "at": datetime.now(timezone.utc).isoformat()})
            return JSONResponse({"ok": True, "result": {"message_id": len(self.messages)}})

        self.app = Starlette(routes=[Route("/bot{token:str}/sendMessage", send, methods=["POST"])])


# =========================================================================== utilidades
def proc_tree_stats(pids: list[int]) -> tuple[float, float]:
    """(cpu %, rss MB) sumando procesos; cpu en % de un núcleo, medido desde la llamada anterior."""
    cpu = rss = 0.0
    for pid in pids:
        try:
            p = _PROCS.setdefault(pid, psutil.Process(pid))
            cpu += p.cpu_percent(None)
            rss += p.memory_info().rss / 1e6
        except psutil.Error:
            _PROCS.pop(pid, None)
    return round(cpu, 1), round(rss, 1)


_PROCS: dict[int, psutil.Process] = {}


def chromium_pids() -> list[int]:
    me = psutil.Process(os.getpid())
    out = []
    for p in me.children(recursive=True):
        try:
            name = p.name().lower()
            if "chrom" in name or "headless" in name:
                out.append(p.pid)
        except psutil.Error:
            pass
    return out


def sim_pids(sims: list[CameraSimulator]) -> list[int]:
    out = []
    for sim in sims:
        if sim._mtx is not None:  # noqa: SLF001 - herramienta de pruebas
            out.append(sim._mtx.pid)  # noqa: SLF001
        for pub in sim._publishers.values():  # noqa: SLF001
            if pub.proc is not None and pub.proc.poll() is None:
                out.append(pub.proc.pid)
    return out


def slope_mb_per_min(samples: list[tuple[float, float]]) -> float:
    if len(samples) < 3:
        return 0.0
    n = len(samples)
    mx = sum(t for t, _ in samples) / n
    my = sum(v for _, v in samples) / n
    den = sum((t - mx) ** 2 for t, _ in samples) or 1.0
    return round(sum((t - mx) * (v - my) for t, v in samples) / den * 60.0, 2)


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ffprobe(path: Path) -> dict[str, Any]:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                          "format=duration:stream=codec_name,width,height,nb_read_frames", "-count_frames",
                          "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[:300]}
    data: dict[str, Any] = json.loads(out.stdout)
    return data


class Walls:
    """Los 4 monitores: una pestaña de Chromium por muro, con sesión de kiosco."""

    def __init__(self, base_url: str, kiosk_token: str) -> None:
        from playwright.sync_api import sync_playwright

        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        self.base_url = base_url
        self.kiosk_token = kiosk_token
        self.ctx = self.browser.new_context(base_url=base_url, viewport={"width": 1920, "height": 1080},
                                            locale="es-ES", timezone_id="Europe/Madrid")
        self.ctx.add_init_script(PC_TRACKER)
        self.pages: dict[int, Any] = {}
        self.errors: dict[int, list[str]] = {}

    def open(self, monitor: int) -> Any:
        page = self.ctx.new_page()
        errs: list[str] = []
        page.on("pageerror", lambda exc: errs.append(f"pageerror: {exc}"))
        page.on("console", lambda m: errs.append(f"console: {m.text}") if m.type == "error" else None)
        page.goto(f"/api/auth/kiosk?token={quote(self.kiosk_token)}&next=/wall/{monitor}")
        page.wait_for_url(f"**/wall/{monitor}")
        page.wait_for_function("() => window.__vmsWall && window.__vmsWall.cells().length > 0", timeout=20000)
        self.pages[monitor] = page
        self.errors[monitor] = errs
        return page

    def cells(self, monitor: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self.pages[monitor].evaluate(CELLS_JS)
        return result

    def playing(self, monitor: int, gap: float = 2.5) -> list[dict[str, Any]]:
        """Estado de cada celda asignada: imagen (videoWidth) y tiempo que avanza de verdad."""
        a = {c["index"]: c for c in self.cells(monitor)}
        time.sleep(gap)
        out = []
        for c in self.cells(monitor):
            prev = a.get(c["index"], c)
            c["advance"] = round(c["t"] - prev["t"], 2)
            c["playing"] = c["w"] > 0 and c["advance"] > gap * 0.5 and c["state"] == "live"
            out.append(c)
        return out

    def wait_all_playing(self, monitor: int, timeout: float) -> tuple[float, list[dict[str, Any]]]:
        t0 = time.monotonic()
        last: list[dict[str, Any]] = []
        while time.monotonic() - t0 < timeout:
            last = self.playing(monitor)
            if last and all(c["playing"] for c in last):
                return time.monotonic() - t0, last
        raise TimeoutError(f"Muro {monitor}: no todas las celdas reproducen tras {timeout:.0f} s: {last}")

    def pc_open(self, monitor: int) -> int:
        return int(self.pages[monitor].evaluate("() => window.__pcTracker ? window.__pcTracker.open : -1"))

    def close(self) -> None:
        try:
            self.ctx.close()
            self.browser.close()
        finally:
            self.pw.stop()


# =========================================================================== la prueba
class SystemCheck:
    def __init__(self, record_seconds: float, load_seconds: float, load_cams: int) -> None:
        self.record_seconds = record_seconds
        self.load_seconds = load_seconds
        self.load_cams = load_cams
        self.r = Results()
        self.pg: EmbeddedPostgres | None = None
        self.simenv: Any = None
        self.load_sim: CameraSimulator | None = None
        self.load_api: MockHttpServer | None = None
        self.tg_srv: MockHttpServer | None = None
        self.tg = TelegramMock()
        self.services: dict[str, Service] = {}
        self.walls: Walls | None = None
        self.api: BackendClient | None = None
        self.cams: dict[str, dict[str, Any]] = {}
        self.door_cam = ""
        self.env: dict[str, str] = {}
        self.values: dict[str, str] = {}

    # ------------------------------------------------------------------ preparación
    def setup(self) -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)
        SHOTS.mkdir(parents=True, exist_ok=True)
        for f in SHOTS.glob("*.png"):
            f.unlink()
        self.pg = EmbeddedPostgres(WORK / "pgdata")
        dsn = self.pg.start()
        self.tg_srv = MockHttpServer(self.tg.app).start()
        http_port = free_port()
        self.mtx_api_port = free_port()
        self.values = {
            "VMS_DATA_DIR": str(WORK / "data"),
            "VMS_SITE_ID": SITE_ID,
            "VMS_LOG_LEVEL": "INFO",
            "VMS_HTTP_HOST": "127.0.0.1",
            "VMS_HTTP_PORT": str(http_port),
            "VMS_ADMIN_INITIAL_PASSWORD": "Admin#E2e-2026",
            "VMS_KIOSK_TOKEN": "kiosco-e2e-" + os.urandom(8).hex(),
            "VMS_CREDENTIAL_BACKEND": "file",
            "VMS_MTX_RTSP_ADDRESS": f"127.0.0.1:{free_port()}",
            "VMS_MTX_WEBRTC_ADDRESS": f"127.0.0.1:{free_port()}",
            "VMS_MTX_WEBRTC_ICE_UDP": f":{free_port()}",
            "VMS_MTX_WEBRTC_ICE_TCP": "off",
            "VMS_MTX_API_ADDRESS": f"127.0.0.1:{self.mtx_api_port}",
            "VMS_MTX_PLAYBACK_ADDRESS": f"127.0.0.1:{free_port()}",
            "VMS_MTX_METRICS_ADDRESS": f"127.0.0.1:{free_port()}",
            "VMS_PG_DSN": dsn,
            "VMS_HEARTBEAT_SECONDS": "10",
            "VMS_TELEGRAM_BOT_TOKEN": TG_TOKEN,
            "VMS_LLM_PROVIDER": "none",
            "VMS_ANALYTICS_MODELS_DIR": str(ROOT / "models"),
            "VMS_ANALYTICS_TELEGRAM_API_BASE": self.tg_srv.base_url,
            "VMS_ANALYTICS_CONFIG_POLL_SECONDS": "5",
            "VMS_CENTRAL_DATA_DIR": str(WORK / "central"),
            "VMS_CENTRAL_HTTP_HOST": "127.0.0.1",
            "VMS_CENTRAL_HTTP_PORT": str(free_port()),
            "VMS_CENTRAL_ADMIN_INITIAL_PASSWORD": "Central#E2e-2026",
        }
        write_env_file(WORK / "e2e.env", self.values)
        self.env = base_env(WORK / "e2e.env")
        self.base_url = f"http://127.0.0.1:{http_port}"

    def start_backend(self) -> None:
        svc = Service("backend", python_cmd("-m", "vms"), self.env, WORK / "logs" / "backend.out.log").start()
        self.services["backend"] = svc
        wait_until(lambda: httpx.get(f"{self.base_url}/api/health", timeout=2).json()["engine"]["running"], 60,
                   "backend con motor en marcha")

    def mtx_api_auth(self) -> tuple[str, str]:
        """Usuario interno del backend ante MediaMTX (derivado del token interno, vms.core.mtx_auth)."""
        token = (WORK / "data" / "secrets" / "internal.token").read_text(encoding="utf-8").strip()
        return MtxCredentials.from_internal_token(token).api_auth

    def mtx_pid(self) -> int:
        f = WORK / "data" / "mediamtx" / "mediamtx.pid"
        return int(f.read_text(encoding="utf-8").split()[0])

    # ------------------------------------------------------------------ a
    def step_a(self) -> None:
        s = self.r.step("a", "Simulador: 4 cámaras (2 estilo Hikvision, 2 estilo Dahua) + 1 cámara con vídeo de personas")
        t0 = time.monotonic()
        self.simenv = start_simulator(WORK, hik_channels=2, dah_channels=2, people_cameras=1)
        sim = self.simenv.sim
        s.details = {"devices": {d.name: {"vendor": d.vendor, "channels": d.channels, "rtsp_port": d.port,
                                          "native_main": d.native_path(1, "main"), "native_sub": d.native_path(1, "sub")}
                                 for d in sim.devices.values()},
                     "hik_http_api_port": self.simenv.hik_api.port, "dah_http_api_port": self.simenv.dah_api.port,
                     "flows_ready": len(sim.ready_paths())}
        s.ok = s.details["flows_ready"] == 10
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ b
    def step_b(self) -> None:
        s = self.r.step("b", "Arranque del sistema, login, alta de cámaras por la API y muros 1-4 con layouts distintos")
        t0 = time.monotonic()
        self.start_backend()
        s.details["backend_ready_s"] = round(time.monotonic() - t0, 1)
        r = httpx.post(f"{self.base_url}/api/auth/login", json={"username": "admin", "password": "mala-clave"},
                       headers=HEADERS)
        s.details["login_bad_password"] = r.status_code
        self.api = BackendClient(self.base_url, "admin", self.values["VMS_ADMIN_INITIAL_PASSWORD"])
        s.details["login_ok"] = True
        sim = self.simenv.sim
        devs = {}
        for name, vendor, dev, http_port in (("NVR Hikvision", "hikvision", "hik1", self.simenv.hik_api.port),
                                             ("NVR Dahua", "dahua", "dah1", self.simenv.dah_api.port)):
            test = self.api.post("/api/devices/test", {"name": name, "vendor": vendor, "kind": "nvr", "host": "127.0.0.1",
                                                       "http_port": http_port, "rtsp_port": sim.device(dev).port,
                                                       "username": "admin", "password": DEFAULT_PASSWORD}, expect=200)
            out = self.api.post("/api/devices", {"name": name, "vendor": vendor, "kind": "nvr", "host": "127.0.0.1",
                                                 "http_port": http_port, "rtsp_port": sim.device(dev).port,
                                                 "username": "admin", "password": DEFAULT_PASSWORD,
                                                 "import_channels": "all"})
            devs[vendor] = {"test_ok": test["ok"], "rtsp_ok": test["rtsp_ok"], "model": out.get("model"),
                            "cameras": len(out["cameras"]), "import_error": out.get("details", {}).get("import_error")}
        door = self.api.post("/api/devices", {"name": "Cámara puerta", "vendor": "generic", "kind": "camera",
                                              "host": "127.0.0.1", "http_port": free_port(),
                                              "rtsp_port": sim.device("door1").port, "username": "admin",
                                              "password": DEFAULT_PASSWORD})
        cam = self.api.post("/api/cameras", {"device_id": door["id"], "channel": 1, "name": "Puerta principal",
                                             "main_path": "/ch1/main", "sub_path": "/ch1/sub"})
        self.door_cam = cam["id"]
        devs["generic"] = {"cameras": 1}
        s.details["devices"] = devs
        cams = self.api.get("/api/cameras")
        self.cams = {c["id"]: c for c in cams}
        s.details["cameras"] = [{"id": c["id"], "name": c["name"], "vendor": c["vendor"]} for c in cams]
        ids = [c["id"] for c in cams if c["id"] != self.door_cam]
        layouts = {1: (4, ids[:4]), 2: (1, [self.door_cam]), 3: (9, [self.door_cam, *ids]),
                   4: (16, [*ids, self.door_cam])}
        for monitor, (grid, chosen) in layouts.items():
            cells: list[str | None] = [*chosen, *([None] * (16 - len(chosen)))]
            self.api.put(f"/api/walls/{monitor}", {"name": f"Monitor {monitor}", "grid": grid, "cells": cells})
        s.details["walls"] = {m: {"grid": g, "cameras": len(c)} for m, (g, c) in layouts.items()}

        def all_online() -> bool:
            st = self.api.get("/api/status")  # type: ignore[union-attr]
            return st["status"] == "ok" and all(c["online"] and c["recording"] for c in st["cameras"])

        s.details["all_online_recording_s"] = round(wait_until(all_online, 60, "5 cámaras en línea y grabando"), 1)
        st = self.api.get("/api/status")
        s.details["status"] = st["status"]
        s.ok = (s.details["login_bad_password"] == 401 and len(cams) == 5 and st["status"] == "ok"
                and all(d.get("import_error") is None for d in devs.values()))
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ g (configuración previa)
    def configure_analytics(self) -> None:
        assert self.api is not None
        self.api.http.patch("/api/settings", json={"alerts": {"telegram_enabled": True, "telegram_chat_id": TG_CHAT},
                                                   "site": {"name": "Tienda E2E", "timezone": "Europe/Madrid"}}
                            ).raise_for_status()
        self.line = self.api.post("/api/analytics/rules", {"kind": "line", "camera_id": self.door_cam,
                                                           "name": "Entrada", "start": [0.02, 0.5], "end": [0.98, 0.5]})
        self.zone = self.api.post("/api/analytics/rules", {
            "kind": "zone", "camera_id": self.door_cam, "name": "Cola cajas",
            "polygon": [[0.0, 0.55], [1.0, 0.55], [1.0, 1.0], [0.0, 1.0]],
            "alert_threshold": 3, "alert_min_seconds": 3, "alert_cooldown_seconds": 60})
        self.api.put(f"/api/analytics/cameras/{self.door_cam}", {"enabled": True, "fps": 10, "detector": "rfdetr-nano",
                                                                 "confidence": 0.4})
        self.services["analytics"] = Service("analytics", python_cmd("-m", "analytics"), self.env,
                                             WORK / "logs" / "analytics.out.log").start()
        self.analytics_started = time.monotonic()

    # ------------------------------------------------------------------ c
    def step_c(self) -> None:
        s = self.r.step("c", "Playwright: /wall/1 a /wall/4 con vídeo WebRTC en todas las celdas asignadas")
        t0 = time.monotonic()
        self.walls = Walls(self.base_url, self.values["VMS_KIOSK_TOKEN"])
        for m in (1, 2, 3, 4):
            self.walls.open(m)
        ok = True
        for m in (1, 2, 3, 4):
            took, cells = self.walls.wait_all_playing(m, 60)
            s.details[f"wall{m}"] = {"cells": len(cells), "all_playing_after_s": round(took, 1),
                                     "videoWidth": sorted({c["w"] for c in cells}),
                                     "currentTime_advance_2.5s": [c["advance"] for c in cells],
                                     "peer_connections_open": self.walls.pc_open(m)}
            ok = ok and all(c["playing"] for c in cells)
            self.walls.pages[m].screenshot(path=str(SHOTS / f"c-muro-{m}.png"))
        sessions = httpx.get(f"http://127.0.0.1:{self.mtx_api_port}/v3/webrtcsessions/list", timeout=5,
                             auth=self.mtx_api_auth()).json()
        s.details["mediamtx_webrtc_sessions"] = sessions.get("itemCount")
        # La API de MediaMTX no admite peticiones anónimas (mostraría las contraseñas de los NVR)
        anon = httpx.get(f"http://127.0.0.1:{self.mtx_api_port}/v3/config/paths/list", timeout=5)
        s.details["mediamtx_api_anonymous_status"] = anon.status_code
        ok = ok and anon.status_code == 401 and DEFAULT_PASSWORD not in anon.text
        # doble clic: celda ampliada con el flujo principal
        page = self.walls.pages[1]
        page.dblclick(".cell[data-index='0']")
        deadline = time.monotonic() + 30
        big: dict[str, Any] = {}
        while time.monotonic() < deadline:
            big = next(c for c in self.walls.cells(1) if c["index"] == 0)
            if big["stream"] == "main" and big["w"] == 640 and big["state"] == "live":
                break
            time.sleep(0.5)
        s.details["dblclick_main_stream"] = {"stream": big.get("stream"), "videoWidth": big.get("w")}
        page.screenshot(path=str(SHOTS / "c-muro-1-ampliado.png"))
        page.keyboard.press("Escape")   # vuelve a la rejilla: las demás celdas se reanudan
        page.wait_for_function("() => window.__vmsWall.expanded < 0", timeout=10000)
        took, cells = self.walls.wait_all_playing(1, 60)
        s.details["back_to_grid_all_playing_s"] = round(took, 1)
        js_errors = {m: e for m, e in self.walls.errors.items() if e}
        s.details["js_errors"] = js_errors
        s.ok = ok and big.get("w") == 640
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ d
    def step_d(self) -> None:
        assert self.api is not None and self.walls is not None
        s = self.r.step("d", f"Grabación de ~{self.record_seconds:.0f} s, línea de tiempo, MP4 válido y reproducción en la interfaz")
        t0 = time.monotonic()
        log.info("Grabando %.0f s...", self.record_seconds)
        time.sleep(self.record_seconds)
        timelines = {}
        ok = True
        for cid in self.cams:
            spans = self.api.get(f"/api/recordings/{cid}/timeline")["spans"]
            total = round(sum(x["duration"] for x in spans), 1)
            timelines[cid] = {"spans": len(spans), "seconds": total, "first_start": spans[0]["start"] if spans else None}
            ok = ok and total >= self.record_seconds * 0.8 and spans[0]["start"].endswith("Z")
        s.details["timelines"] = timelines
        cid = next(iter(self.cams))
        spans = self.api.get(f"/api/recordings/{cid}/timeline")["spans"]
        start = parse_iso(spans[0]["start"]) + timedelta(seconds=30)
        r = self.api.http.get(f"/api/recordings/{cid}/video", params={
            "start": start.isoformat().replace("+00:00", "Z"), "duration": 30, "format": "mp4", "download": 1})
        mp4 = WORK / "clip.mp4"
        mp4.write_bytes(r.content)
        probe = ffprobe(mp4)
        st0 = (probe.get("streams") or [{}])[0]
        s.details["clip"] = {"http": r.status_code, "content_type": r.headers.get("content-type"),
                             "content_disposition": r.headers.get("content-disposition"), "bytes": len(r.content),
                             "ffprobe_codec": st0.get("codec_name"), "width": st0.get("width"),
                             "height": st0.get("height"), "frames": st0.get("nb_read_frames"),
                             "duration_s": (probe.get("format") or {}).get("duration"), "ffprobe_error": probe.get("error")}
        ok = ok and r.status_code == 200 and st0.get("codec_name") == "h264" and \
            abs(float((probe.get("format") or {}).get("duration", 0)) - 30) < 2
        # reproducción en la interfaz (usuario operador/admin, página /playback)
        page = self.walls.ctx.browser.new_context(base_url=self.base_url, viewport={"width": 1600, "height": 900},
                                                  locale="es-ES", timezone_id="Europe/Madrid").new_page()
        self.admin_page = page
        page.goto("/login?next=/playback")
        page.fill("#username", "admin")
        page.fill("#password", self.values["VMS_ADMIN_INITIAL_PASSWORD"])
        page.click("#login-submit")
        page.wait_for_url(lambda url: "/login" not in url and url.endswith("/playback"))
        page.goto(f"/playback?camera={cid}")
        page.wait_for_function("() => window.__vmsPlayback && window.__vmsPlayback.spans.length > 0", timeout=30000)
        page.click("#pb-zoom [data-hours='1']")
        span = page.evaluate("() => window.__vmsPlayback.spans[0]")
        view = page.evaluate("() => window.__vmsPlayback.view")
        ts = {k: parse_iso(v).timestamp() for k, v in
              {"v0": view["from"], "v1": view["to"], "s0": span["start"]}.items()}
        target = ts["s0"] + 20
        box = page.locator("#timeline-track").bounding_box()
        page.mouse.click(box["x"] + (target - ts["v0"]) / (ts["v1"] - ts["v0"]) * box["width"],
                         box["y"] + box["height"] / 2)
        info: dict[str, Any] = {}
        deadline = time.monotonic() + 30
        first = None
        while time.monotonic() < deadline:
            info = page.eval_on_selector("#pb-video", "v => ({t: v.currentTime, w: v.videoWidth, h: v.videoHeight, paused: v.paused})")
            if info["w"] > 0:
                if first is None:
                    first = info
                elif info["t"] - first["t"] > 1.5:
                    break
            time.sleep(0.5)
        s.details["ui_playback"] = {**info, "advanced": round(info["t"] - (first or info)["t"], 2)}
        page.screenshot(path=str(SHOTS / "d-reproduccion.png"))
        ok = ok and info.get("w") == 640 and s.details["ui_playback"]["advanced"] > 1.5
        s.ok = ok
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ e
    def step_e(self) -> None:
        assert self.api is not None and self.walls is not None
        s = self.r.step("e", "Corte de 20 s de una cámara simulada (principal y subflujo) y recuperación sola")
        t0 = time.monotonic()
        sim = self.simenv.sim
        cid = next(c for c, v in self.cams.items() if v["name"] == "Entrada")
        cell_idx = next(c["index"] for c in self.walls.cells(1) if c["camera"] == cid)
        killed_at = datetime.now(timezone.utc)
        sim.kill_stream("hik1", 1, "main")
        sim.kill_stream("hik1", 1, "sub")

        def cam_live(value: bool) -> bool:
            live = self.api.get(f"/api/cameras/{cid}")["live"]  # type: ignore[union-attr]
            return bool(live["online"]) is value and bool(live["recording"]) is value

        s.details["api_detects_cut_s"] = round(wait_until(lambda: cam_live(False), 30, "detectar el corte"), 1)
        st = self.api.get("/api/status")
        s.details["status_during_cut"] = {"status": st["status"], "problems": st["problems"]}
        time.sleep(max(0.0, 20 - (time.monotonic() - t0)))
        cell = next(c for c in self.walls.cells(1) if c["index"] == cell_idx)
        s.details["wall_cell_state_during_cut"] = cell["state"]
        self.walls.pages[1].screenshot(path=str(SHOTS / "e-muro-1-corte.png"))
        s.details["cut_seconds"] = round(time.monotonic() - t0, 1)
        restart = time.monotonic()
        sim.start_stream("hik1", 1, "main")
        sim.start_stream("hik1", 1, "sub")
        s.details["api_recovers_s"] = round(wait_until(lambda: cam_live(True), 60, "reconexión vista por la API"), 1)

        def wall_ok() -> bool:
            c = next(x for x in self.walls.playing(1, gap=1.5) if x["index"] == cell_idx)  # type: ignore[union-attr]
            return bool(c["playing"])

        s.details["wall_recovers_s"] = round(wait_until(wall_ok, 90, "celda del muro en vivo otra vez", 0.5)
                                             + (time.monotonic() - restart) * 0, 1)
        s.details["wall_recovers_since_restart_s"] = round(time.monotonic() - restart, 1)
        self.walls.pages[1].screenshot(path=str(SHOTS / "e-muro-1-recuperado.png"))
        time.sleep(8)
        spans = self.api.get(f"/api/recordings/{cid}/timeline")["spans"]
        after = [x for x in spans if parse_iso(x["end"]) > killed_at + timedelta(seconds=15)]
        gap = None
        for a, b in zip(spans, spans[1:], strict=False):
            g = (parse_iso(b["start"]) - parse_iso(a["end"])).total_seconds()
            if g > 5:
                gap = round(g, 1)
        s.details["recording"] = {"spans": len(spans), "gap_seconds": gap,
                                  "recording_after_restart_s": round(sum(x["duration"] for x in after), 1)}
        others = [c for c in self.walls.playing(1) if c["index"] != cell_idx]
        s.details["other_cells_unaffected"] = all(c["playing"] for c in others)
        s.ok = (s.details["recording"]["recording_after_restart_s"] > 3 and gap is not None
                and s.details["other_cells_unaffected"] and s.details["wall_cell_state_during_cut"] != "live")
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ f
    def step_f(self) -> None:
        assert self.api is not None and self.walls is not None
        s = self.r.step("f", "MediaMTX matado (SIGKILL): el supervisor del backend lo relanza y todo vuelve")
        t0 = time.monotonic()
        before = self.api.get("/api/status")["engine"]
        pid = self.mtx_pid()
        os.kill(pid, signal.SIGKILL)
        killed = time.monotonic()

        def relaunched() -> bool:
            eng = self.api.get("/api/status")["engine"]  # type: ignore[union-attr]
            return bool(eng["running"]) and eng["restarts"] > before["restarts"]

        s.details["engine_relaunched_s"] = round(wait_until(relaunched, 60, "MediaMTX relanzado"), 1)
        new_pid = self.mtx_pid()
        s.details["pids"] = {"before": pid, "after": new_pid}

        def all_online() -> bool:
            st = self.api.get("/api/status")  # type: ignore[union-attr]
            return all(c["online"] and c["recording"] for c in st["cameras"])

        s.details["all_cameras_online_s"] = round(wait_until(all_online, 90, "cámaras en línea tras relanzar") +
                                                  (time.monotonic() - killed) * 0, 1)
        s.details["all_cameras_online_since_kill_s"] = round(time.monotonic() - killed, 1)
        walls_ok = {}
        for m in (1, 2, 3, 4):
            took, _ = self.walls.wait_all_playing(m, 120)
            walls_ok[m] = round(time.monotonic() - killed, 1)
        s.details["walls_live_since_kill_s"] = walls_ok
        eng = self.api.get("/api/status")["engine"]
        s.details["engine_after"] = {"running": eng["running"], "restarts": eng["restarts"]}
        s.details["old_pid_alive"] = psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        self.walls.pages[3].screenshot(path=str(SHOTS / "f-muro-3-tras-relanzar.png"))
        s.ok = new_pid != pid and eng["running"] and not s.details["old_pid_alive"]
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ g
    def step_g(self) -> None:
        assert self.api is not None and self.pg is not None
        s = self.r.step("g", "Analítica real (RF-DETR nano): conteos por minuto en PostgreSQL, alerta de cola por "
                             "Telegram simulado e informe semanal con plantilla")
        t0 = time.monotonic()
        import psycopg

        # al menos 3 minutos completos de analítica para tener minutos cerrados
        wait_s = max(0.0, 200 - (time.monotonic() - self.analytics_started))
        if wait_s:
            log.info("Esperando %.0f s más de analítica...", wait_s)
            time.sleep(wait_s)
        st = self.api.get("/api/analytics/status")
        cam = next((c for c in st.get("cameras", []) if c["camera_id"] == self.door_cam), {})
        s.details["analytics_status"] = {k: cam.get(k) for k in (
            "state", "fps_target", "fps_in", "fps_processed", "inference_ms_p50", "inference_ms_p95",
            "frames_processed", "reconnects", "detector", "frame_size", "totals", "last_error")}
        s.details["analytics_status"]["stale"] = st.get("stale")
        s.details["analytics_status"]["db"] = st.get("db")
        dsn = self.pg.dsn
        with psycopg.connect(dsn) as conn:
            lc = conn.execute("SELECT count(*), coalesce(sum(count_in),0), coalesce(sum(count_out),0), "
                              "min(minute), max(minute) FROM line_counts_minute WHERE site_id = %s",
                              (SITE_ID,)).fetchone()
            zo = conn.execute("SELECT count(*), coalesce(max(max_people),0), coalesce(sum(samples),0), "
                              "round(avg(avg_people)::numeric, 2), coalesce(sum(seconds_over_threshold),0) "
                              "FROM zone_occupancy_minute WHERE site_id = %s", (SITE_ID,)).fetchone()
            qa = conn.execute("SELECT count(*), count(notified_at), count(ended_at), coalesce(max(peak_people),0) "
                              "FROM queue_alerts WHERE site_id = %s", (SITE_ID,)).fetchone()
            meta = conn.execute("SELECT (SELECT name FROM sites WHERE site_id = %s), "
                                "(SELECT count(*) FROM site_cameras WHERE site_id = %s), "
                                "(SELECT count(*) FROM analytics_rules WHERE site_id = %s)",
                                (SITE_ID, SITE_ID, SITE_ID)).fetchone()
            per_min = conn.execute("SELECT minute, count_in, count_out FROM line_counts_minute WHERE site_id = %s "
                                   "ORDER BY minute", (SITE_ID,)).fetchall()
        assert lc and zo and qa and meta
        s.details["line_counts_minute"] = {"rows": lc[0], "in": lc[1], "out": lc[2],
                                           "first_minute": str(lc[3]), "last_minute": str(lc[4]),
                                           "per_minute": [(str(m), i, o) for m, i, o in per_min]}
        s.details["zone_occupancy_minute"] = {"rows": zo[0], "max_people": zo[1], "samples": zo[2],
                                              "avg_people": float(zo[3] or 0), "seconds_over_threshold": zo[4]}
        s.details["queue_alerts"] = {"rows": qa[0], "notified": qa[1], "ended": qa[2], "peak_people": qa[3]}
        s.details["sites_cameras_rules"] = {"site_name": meta[0], "site_cameras": meta[1], "rules": meta[2]}
        msgs = self.tg.messages
        s.details["telegram_mock"] = {"messages": len(msgs), "token_ok": all(m["token_ok"] for m in msgs),
                                      "chat_ok": all(str(m.get("chat_id")) == TG_CHAT for m in msgs),
                                      "first_text": (msgs[0]["text"][:220] if msgs else None),
                                      "has_photo": any("photo" in m for m in msgs)}
        # RGPD: ninguna imagen ni vídeo fuera de las grabaciones del VMS
        bad = [str(p.relative_to(WORK)) for p in (WORK / "data").rglob("*")
               if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".h264"}
               or (p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".avi"} and "recordings" not in p.parts)]
        s.details["rgpd_image_files_outside_recordings"] = bad
        # informe semanal de la semana actual (lunes en hora de Madrid), solo plantilla
        today = datetime.now(MADRID).date()
        monday = today - timedelta(days=today.weekday())
        out_dir = WORK / "informes"
        rep = subprocess.run(python_cmd("-m", "analytics.reports", "--site", SITE_ID, "--week", monday.isoformat(),
                                        "--no-llm", "--out", str(out_dir), "--dsn", dsn),
                             cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120)
        with psycopg.connect(dsn) as conn:
            wr = conn.execute("SELECT status, provider, length(body_markdown), error FROM weekly_reports "
                              "WHERE site_id = %s AND week_start = %s", (SITE_ID, monday)).fetchone()
        md = sorted(out_dir.glob("*.md"))
        s.details["weekly_report"] = {"exit_code": rep.returncode, "stdout": rep.stdout.strip()[-300:],
                                      "stderr": rep.stderr.strip()[-300:], "week_start": monday.isoformat(),
                                      "db_row": {"status": wr[0], "provider": wr[1], "markdown_chars": wr[2],
                                                 "error": wr[3]} if wr else None,
                                      "files": [p.name for p in out_dir.glob("*")]}
        if md:
            shutil.copy(md[0], SHOTS.parent / "informe-semanal-ejemplo.md")
            s.details["weekly_report"]["markdown_head"] = md[0].read_text(encoding="utf-8")[:900]
        if meta[0] != "Tienda E2E":
            s.notes.append(f"El nombre de la sede en PostgreSQL es «{meta[0]}», no el puesto en el panel")
        s.ok = (meta[0] == "Tienda E2E" and lc[0] >= 2 and (lc[1] + lc[2]) > 0 and zo[0] >= 2 and qa[0] >= 1 and qa[1] >= 1 and len(msgs) >= 1
                and not bad and rep.returncode == 0 and wr is not None and wr[0] == "ok" and wr[1] == "template")
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ h
    def step_h(self) -> None:
        assert self.api is not None and self.walls is not None
        s = self.r.step("h", "Panel central: recibe el latido (directo a PostgreSQL y por el agente HTTP) y muestra la sede")
        t0 = time.monotonic()
        port = self.values["VMS_CENTRAL_HTTP_PORT"]
        curl = f"http://127.0.0.1:{port}"
        self.services["central"] = Service("central", python_cmd("-m", "central"), self.env,
                                           WORK / "logs" / "central.out.log").start()
        wait_until(lambda: httpx.get(f"{curl}/api/health", timeout=2).status_code == 200, 60, "panel central")
        c = httpx.Client(base_url=curl, headers=HEADERS, timeout=15)
        c.post("/api/auth/login", json={"username": "admin", "password": self.values["VMS_CENTRAL_ADMIN_INITIAL_PASSWORD"]}
               ).raise_for_status()

        def site_seen() -> bool:
            sites = c.get("/api/sites").json()["sites"]
            return any(x.get("site_id") == SITE_ID and x.get("online") for x in sites)

        s.details["direct_heartbeat_seen_s"] = round(wait_until(site_seen, 40, "sede en el panel central"), 1)
        site = next(x for x in c.get("/api/sites").json()["sites"] if x["site_id"] == SITE_ID)
        s.details["site_direct"] = {k: site.get(k) for k in ("site_id", "name", "online", "state", "last_seen",
                                                              "cameras_total", "cameras_online", "version", "hostname",
                                                              "analytics_running", "today")}
        # agente HTTP: token de sede + usuario operador del backend
        tok = c.post(f"/api/site-tokens/{SITE_ID}").json()
        token = tok.get("token") or ""
        self.api.post("/api/users", {"username": "latido", "password": "Latido#2026x", "role": "operator"})
        agent_env = {**self.env, "VMS_CENTRAL_URL": curl, "VMS_SITE_TOKEN": token, "VMS_AGENT_USERNAME": "latido",
                     "VMS_AGENT_PASSWORD": "Latido#2026x", "VMS_AGENT_BACKEND_URL": self.base_url}
        before = site.get("last_seen")
        ag = subprocess.run(python_cmd("-m", "central.agent", "--once"), cwd=ROOT, env=agent_env,
                            capture_output=True, text=True, timeout=60)
        site2 = next(x for x in c.get("/api/sites").json()["sites"] if x["site_id"] == SITE_ID)
        detail = c.get(f"/api/sites/{SITE_ID}").json()
        s.details["agent"] = {"exit_code": ag.returncode, "token_issued": bool(token),
                              "stderr_tail": ag.stderr.strip()[-300:], "last_seen_before": before,
                              "last_seen_after": site2.get("last_seen"), "state_after": site2.get("state"),
                              "detail_cameras": len(detail.get("cameras") or []),
                              "detail_rules": len(detail.get("rules") or [])}
        counts = c.get(f"/api/sites/{SITE_ID}/counts", params={"range": "today", "bucket": "hour"}).json()
        s.details["central_counts_today"] = counts if len(json.dumps(counts)) < 1500 else str(counts)[:1500]
        # captura del panel central
        page = self.walls.browser.new_context(base_url=curl, viewport={"width": 1440, "height": 900},
                                              locale="es-ES", timezone_id="Europe/Madrid").new_page()
        page.goto("/login")
        page.fill("#username", "admin")
        page.fill("#password", self.values["VMS_CENTRAL_ADMIN_INITIAL_PASSWORD"])
        page.click("button[type=submit]")
        page.wait_for_url(lambda u: "/login" not in u)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(SHOTS / "h-central-sedes.png"), full_page=True)
        page.goto(f"/sites/{SITE_ID}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(SHOTS / "h-central-sede.png"), full_page=True)
        s.details["central_ui_text_has_site"] = SITE_ID in page.content() or "Tienda E2E" in page.content()
        c.close()
        s.ok = (bool(site["online"]) and ag.returncode == 0
                and site2.get("last_seen") != before and s.details["central_ui_text_has_site"])
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ i
    def step_i(self) -> None:
        assert self.api is not None and self.walls is not None
        n = self.load_cams
        s = self.r.step("i", f"Carga: {n} flujos simulados en un muro 4x4 durante {self.load_seconds:.0f} s "
                             "(CPU y memoria de backend, MediaMTX, navegador)")
        t0 = time.monotonic()
        self.load_sim = CameraSimulator([SimDevice("load", "hikvision", channels=n)], WORK / "camsim-load").start(wait_ready=90)
        self.load_api = MockHttpServer(HikvisionMock(password=DEFAULT_PASSWORD, channels=[
            HikChannel(f"Carga {i + 1}", f"192.168.253.{i + 2}") for i in range(n)]).app).start()
        out = self.api.post("/api/devices", {"name": "NVR carga 16", "vendor": "hikvision", "kind": "nvr",
                                             "host": "127.0.0.1", "http_port": self.load_api.port,
                                             "rtsp_port": self.load_sim.device("load").port, "username": "admin",
                                             "password": DEFAULT_PASSWORD, "import_channels": "all"})
        load_ids = out["cameras"]
        s.details["load_cameras"] = len(load_ids)
        for m in (1, 2, 3):   # solo queda abierto el muro de carga
            self.walls.pages[m].close()
        self.api.put("/api/walls/4", {"name": "Carga 4x4", "grid": 16, "cells": load_ids[:16]})

        def recording_all() -> bool:
            st = self.api.get("/api/status")  # type: ignore[union-attr]
            return sum(1 for c in st["cameras"] if c["online"] and c["recording"]) >= 5 + n

        s.details["all_recording_s"] = round(wait_until(recording_all, 90, "todas las cámaras grabando"), 1)
        took, cells = self.walls.wait_all_playing(4, 120)
        s.details["wall4_all_playing_s"] = round(took, 1)
        s.details["start_cells"] = {"playing": sum(c["playing"] for c in cells), "of": len(cells),
                                    "videoWidth": sorted({c["w"] for c in cells})}
        self.walls.pages[4].screenshot(path=str(SHOTS / "i-muro-4x4-inicio.png"))
        frames0 = {c["index"]: c["frames"] for c in cells}
        groups: dict[str, Callable[[], list[int]]] = {
            "backend": lambda: [self.services["backend"].pid],
            "mediamtx": lambda: [self.mtx_pid()],
            "navegador": chromium_pids,
            "analitica": lambda: [self.services["analytics"].pid],
            "simulador": lambda: sim_pids([self.simenv.sim, self.load_sim]),  # type: ignore[list-item]
        }
        for g in groups.values():
            proc_tree_stats(g())  # cebar cpu_percent
        samples: dict[str, list[tuple[float, float, float]]] = {k: [] for k in groups}
        sys_samples: list[tuple[float, float, float]] = []
        psutil.cpu_percent(None)
        start = time.monotonic()
        while time.monotonic() - start < self.load_seconds:
            time.sleep(10)
            t = time.monotonic() - start
            for k, g in groups.items():
                cpu, rss = proc_tree_stats(g())
                samples[k].append((round(t), cpu, rss))
            vm = psutil.virtual_memory()
            sys_samples.append((round(t), psutil.cpu_percent(None), round(vm.used / 1e9, 2)))
        took_end = self.walls.playing(4, gap=3)
        s.details["end_cells"] = {"playing": sum(c["playing"] for c in took_end), "of": len(took_end),
                                  "frames_decoded_during_test": sum(c["frames"] - frames0.get(c["index"], 0)
                                                                    for c in took_end),
                                  "dropped_frames_total": sum(c["dropped"] for c in took_end),
                                  "reconnects_total": sum(c["reconnects"] for c in took_end)}
        s.details["peer_connections_open"] = self.walls.pc_open(4)
        sessions = httpx.get(f"http://127.0.0.1:{self.mtx_api_port}/v3/webrtcsessions/list", timeout=5,
                             auth=self.mtx_api_auth()).json()
        s.details["mediamtx_webrtc_sessions"] = sessions.get("itemCount")
        self.walls.pages[4].screenshot(path=str(SHOTS / "i-muro-4x4-final.png"))
        summary = {}
        for k, rows in samples.items():
            cpus = [c for _, c, _ in rows]
            rss = [m for _, _, m in rows]
            first = rss[: max(1, len(rss) // 5)]
            last = rss[-max(1, len(rss) // 5):]
            summary[k] = {"cpu_avg_pct": round(sum(cpus) / len(cpus), 1), "cpu_max_pct": max(cpus),
                          "rss_start_mb": rss[0], "rss_end_mb": rss[-1],
                          "rss_first_fifth_avg_mb": round(sum(first) / len(first), 1),
                          "rss_last_fifth_avg_mb": round(sum(last) / len(last), 1),
                          "rss_slope_mb_per_min": slope_mb_per_min([(t, m) for t, _, m in rows])}
        s.details["resources"] = summary
        s.details["samples"] = samples
        s.details["system"] = {"cpu_count": psutil.cpu_count(), "cpu_avg_pct_total": round(
            sum(c for _, c, _ in sys_samples) / len(sys_samples), 1),
            "mem_used_gb_start": sys_samples[0][2], "mem_used_gb_end": sys_samples[-1][2]}
        s.ok = (s.details["end_cells"]["playing"] == len(took_end) == n
                and all(summary[k]["rss_slope_mb_per_min"] < 5 for k in ("backend", "mediamtx")))
        s.seconds = round(time.monotonic() - t0, 1)

    # ------------------------------------------------------------------ parada
    def step_shutdown(self) -> None:
        s = self.r.step("z", "Parada ordenada: sin procesos huérfanos y sin contraseñas en disco ni registros")
        t0 = time.monotonic()
        mtx = self.mtx_pid() if (WORK / "data" / "mediamtx" / "mediamtx.pid").is_file() else 0
        for name in ("analytics", "central", "backend"):
            svc = self.services.get(name)
            if svc:
                svc.stop()
                s.details[f"{name}_exit_code"] = svc.proc.returncode if svc.proc else None
        time.sleep(1)
        s.details["mediamtx_orphan"] = bool(mtx) and psutil.pid_exists(mtx) and \
            psutil.Process(mtx).status() != psutil.STATUS_ZOMBIE
        secrets_found = []
        needles = [DEFAULT_PASSWORD, quote(DEFAULT_PASSWORD, safe=""), TG_TOKEN, "Latido#2026x"]
        for p in list((WORK / "data").rglob("*")) + list((WORK / "logs").rglob("*")) + list((WORK / "central").rglob("*")):
            if p.is_file() and p.suffix in {".json", ".log", ".yml", ".yaml", ".txt"} and p.stat().st_size < 50_000_000:
                text = p.read_text(encoding="utf-8", errors="replace")
                for n in needles:
                    if n in text:
                        secrets_found.append(f"{p.relative_to(WORK)}: {n[:6]}…")
        s.details["secrets_in_files"] = secrets_found
        # RGPD art. 32: quién vio o descargó grabaciones y quién amplió una cámara en vivo (paso d y c)
        audit_file = WORK / "data" / "logs" / "audit.log"
        events: dict[str, int] = {}
        if audit_file.is_file():
            for line in audit_file.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(line.split(" ", 2)[-1] if not line.startswith("{") else line)
                except ValueError:
                    continue
                events[ev.get("event", "?")] = events.get(ev.get("event", "?"), 0) + 1
        s.details["audit_events"] = events
        audit_ok = events.get("recording_download", 0) >= 1 and events.get("recording_view", 0) >= 1
        s.ok = not s.details["mediamtx_orphan"] and not secrets_found and audit_ok and \
            all(clean_exit(v) for k, v in s.details.items() if k.endswith("_exit_code"))
        s.seconds = round(time.monotonic() - t0, 1)

    def teardown(self) -> None:
        for name in ("analytics", "central", "backend"):
            svc = self.services.get(name)
            if svc and svc.alive():
                svc.stop()
        if self.walls:
            try:
                self.walls.close()
            except Exception:  # noqa: BLE001
                log.exception("Error al cerrar Chromium")
        for srv in (self.load_api, self.tg_srv):
            if srv:
                srv.stop()
        if self.load_sim:
            self.load_sim.stop()
        if self.simenv:
            self.simenv.stop()
        if self.pg:
            self.pg.stop()

    def run(self, only: set[str] | None = None) -> int:
        steps: list[tuple[str, Callable[[], None]]] = [
            ("a", self.step_a), ("b", self.step_b), ("g0", self.configure_analytics), ("c", self.step_c),
            ("d", self.step_d), ("e", self.step_e), ("f", self.step_f), ("g", self.step_g), ("h", self.step_h),
            ("i", self.step_i), ("z", self.step_shutdown)]
        rc = 0
        try:
            self.setup()
            for key, fn in steps:
                if only and key not in only and key not in ("a", "b", "g0", "c", "z"):
                    continue
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 - se anota el fallo del paso y se sigue si se puede
                    rc = 1
                    log.error("Paso %s falló: %s", key, exc)
                    cur = self.r.steps[-1] if self.r.steps and self.r.steps[-1].key == key else self.r.step(key, key)
                    cur.ok = False
                    cur.notes.append(f"EXCEPCIÓN: {type(exc).__name__}: {exc}")
                    cur.details["traceback"] = traceback.format_exc()[-2000:]
                    if key in ("a", "b"):
                        break
                finally:
                    self.r.save(WORK / "results.json")
        finally:
            self.teardown()
            self.r.save(WORK / "results.json")
        rc = rc or (0 if all(s.ok for s in self.r.steps) else 1)
        for s in self.r.steps:
            log.info("%s) %s → %s (%.0f s)", s.key, s.title, "OK" if s.ok else "FALLO", s.seconds)
        return rc


def render_markdown(results_file: Path, out: Path) -> None:
    """Anexo con los valores medidos tal cual (lo cita tests/e2e/RESULTADOS.md)."""
    data = json.loads(results_file.read_text(encoding="utf-8"))
    lines = ["# Prueba de sistema — datos medidos (generado automáticamente)", "",
             f"Generado por `python -m tests.e2e.system_check` · inicio {data['started_at']} · fin "
             f"{data['finished_at']} · plataforma {data['platform']}", "",
             "| Paso | Descripción | Resultado | Duración |", "|---|---|---|---:|"]
    for st in data["steps"]:
        res = "OK" if st["ok"] else ("FALLO" if st["ok"] is False else "—")
        lines.append(f"| {st['key']} | {st['title']} | **{res}** | {st['seconds']:.0f} s |")
    for st in data["steps"]:
        lines += ["", f"## {st['key']}) {st['title']}", ""]
        for n in st.get("notes", []):
            lines.append(f"- {n}")
        details = {k: v for k, v in st["details"].items() if k not in ("samples", "traceback")}
        lines += ["```json", json.dumps(details, indent=2, ensure_ascii=False, default=str), "```"]
        if "samples" in st["details"]:
            lines += ["", "Muestras cada 10 s (t s, CPU % de un núcleo, RSS MB):", "",
                      "| t | " + " | ".join(st["details"]["samples"]) + " |",
                      "|---:|" + "---|" * len(st["details"]["samples"])]
            series = list(st["details"]["samples"].values())
            for i in range(len(series[0])):
                row = [f"{s_[i][1]:.0f} % · {s_[i][2]:.0f} MB" for s_ in series]
                lines.append(f"| {series[0][i][0]} | " + " | ".join(row) + " |")
        if "traceback" in st["details"]:
            lines += ["", "```", st["details"]["traceback"], "```"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.e2e.system_check")
    p.add_argument("--record-seconds", type=float, default=150)
    p.add_argument("--load-seconds", type=float, default=300)
    p.add_argument("--load-cams", type=int, default=16)
    p.add_argument("--only", help="pasos opcionales a ejecutar (p. ej. «d,e»); a, b, c y la parada siempre")
    p.add_argument("--render-only", action="store_true", help="solo regenerar el anexo desde results.json")
    args = p.parse_args(argv)
    annex = ROOT / "tests" / "e2e" / "RESULTADOS-datos.md"
    if args.render_only:
        render_markdown(WORK / "results.json", annex)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    only = set(args.only.split(",")) if args.only else None
    rc = SystemCheck(args.record_seconds, args.load_seconds, args.load_cams).run(only)
    render_markdown(WORK / "results.json", annex)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
