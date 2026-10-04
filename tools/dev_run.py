"""Arranque y parada de todo el sistema en el equipo de desarrollo (una sola orden).

    python -m tools.dev_run start [--sim] [--seed] [--pg] [--central] [--no-analytics] [--detach]
    python -m tools.dev_run status
    python -m tools.dev_run stop

`start` levanta, en este orden:
  1. PostgreSQL embebido (pgserver, solo con --pg) y aplica las migraciones;
  2. el simulador de cámaras (--sim): 2 NVR estilo Hikvision y Dahua con sus API HTTP simuladas
     y una cámara genérica con el vídeo de personas (tests/assets);
  3. el backend VMS (`python -m vms`), que a su vez arranca y vigila MediaMTX;
  4. la analítica (`python -m analytics`), si hay un modelo exportado en models/;
  5. el panel central (`python -m central`, solo con --central y --pg).
Con --seed da de alta los equipos del simulador por la API y reparte las cámaras en los muros.

Todo queda en `.tmp/dev/` (datos, registros, .env de desarrollo y `run.json` con los procesos).
`stop` pide la parada al supervisor (archivo `stop.request`, funciona igual en Windows y en
Linux/macOS), que para los procesos en orden inverso y comprueba que no queda ningún MediaMTX
huérfano. Si el supervisor no responde, `stop` para los procesos uno a uno.

Herramienta de desarrollo y demostración: no forma parte de la instalación en tienda.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKDIR = ROOT / ".tmp" / "dev"
PEOPLE_VIDEO = ROOT / "tests" / "assets" / "people-walking-h264.mp4"
HEADERS = {"X-Requested-With": "vms"}
STOP_POLL_S = 0.5

log = logging.getLogger("tools.dev_run")


# =========================================================================== utilidades
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def wait_until(check: Callable[[], bool], timeout: float, what: str, interval: float = 0.3) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if check():
                return time.monotonic() - t0
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            pass
        time.sleep(interval)
    raise TimeoutError(f"Tiempo agotado ({timeout:.0f} s) esperando: {what}")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil va en el extra vms
        if sys.platform == "win32":
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _popen_group_kwargs() -> dict[str, Any]:
    """Cada servicio en su propio grupo: Ctrl+C en la consola no les llega dos veces."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def terminate_pid(pid: int, timeout: float = 15.0, name: str = "") -> bool:
    """Parada ordenada (SIGTERM / Ctrl+Break en Windows) y, si no basta, kill. True si terminó."""
    if not pid_alive(pid):
        return True
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log.debug("No se pudo enviar la señal de parada a %s (%d): %s", name, pid, exc)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    log.warning("%s (pid %d) no paró en %.0f s: se fuerza", name or "proceso", pid, timeout)
    try:
        import psutil
        psutil.Process(pid).kill()
    except Exception as exc:  # noqa: BLE001 - puede haber terminado justo ahora
        log.debug("kill de %d: %s", pid, exc)
    time.sleep(0.5)
    return not pid_alive(pid)


@dataclass
class Service:
    """Un proceso del sistema (backend, analítica, central...) con su registro en disco."""

    name: str
    cmd: list[str]
    env: dict[str, str]
    log_file: Path
    proc: subprocess.Popen[bytes] | None = None

    def start(self) -> "Service":
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.log_file, "ab")
        try:
            self.proc = subprocess.Popen(self.cmd, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, stdout=fh,
                                         stderr=subprocess.STDOUT, **_popen_group_kwargs())
        finally:
            fh.close()
        log.info("%s arrancado (pid %d, registro %s)", self.name, self.proc.pid, self.log_file)
        return self

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 20.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            terminate_pid(self.proc.pid, timeout, self.name)
        try:
            code = self.proc.wait(timeout=5)
            if clean_exit(code):
                log.info("%s parado correctamente", self.name)
            else:
                log.warning("%s parado con código %s", self.name, code)
        except subprocess.TimeoutExpired:
            log.error("%s sigue vivo tras pararlo (pid %d)", self.name, self.proc.pid)


def clean_exit(code: int | None) -> bool:
    """0, o la propia señal de parada: uvicorn la vuelve a lanzar tras cerrar ordenadamente (código -15)."""
    return code in (0, None) or (sys.platform != "win32" and code == -signal.SIGTERM)


def python_cmd(*args: str) -> list[str]:
    return [sys.executable, *args]


def base_env(env_file: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Entorno de los servicios: el del usuario sin VMS_* (manda el .env de desarrollo)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VMS_")}
    env["VMS_ENV_FILE"] = str(env_file)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = ["# .env de DESARROLLO generado por tools/dev_run.py (no usar en una tienda)"]
    lines += [f"{k}={v}" for k, v in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o600)


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# =========================================================================== PostgreSQL embebido
class EmbeddedPostgres:
    """pgserver (PostgreSQL embebido en el venv) para desarrollo y pruebas. Producción: PostgreSQL real."""

    def __init__(self, pgdata: Path) -> None:
        self.pgdata = pgdata
        self._srv: Any = None
        self.dsn = ""

    def start(self, dbname: str = "vms") -> str:
        try:
            import pgserver
        except ImportError as exc:
            raise RuntimeError("Falta pgserver en el venv (pip install pgserver); o usa un PostgreSQL real") from exc
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        from vms.db.migrate import apply_migrations

        self.pgdata.mkdir(parents=True, exist_ok=True)
        self._srv = pgserver.get_server(self.pgdata, cleanup_mode="stop")
        admin = self._srv.get_uri()
        with psycopg.connect(admin, autocommit=True) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{dbname}"')
        params = conninfo_to_dict(admin)
        params["dbname"] = dbname
        self.dsn = make_conninfo("", **params)  # type: ignore[arg-type]
        apply_migrations(self.dsn)
        log.info("PostgreSQL embebido listo en %s", self.pgdata)
        return self.dsn

    def stop(self) -> None:
        if self._srv is not None:
            try:
                self._srv.cleanup()
                log.info("PostgreSQL embebido parado")
            except Exception:  # noqa: BLE001 - al parar solo se informa
                log.exception("Error al parar PostgreSQL embebido")
            self._srv = None


# =========================================================================== simulador
@dataclass
class SimEnv:
    """Simulador RTSP + API HTTP simuladas de los NVR (para «Probar conexión» e «Importar canales»)."""

    sim: Any
    hik_api: Any
    dah_api: Any
    hik_channels: int
    dah_channels: int
    generic: list[str] = field(default_factory=list)

    def stop(self) -> None:
        for srv in (self.hik_api, self.dah_api):
            try:
                srv.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error al parar una API simulada")
        self.sim.stop()

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"devices": {}}
        for d in self.sim.devices.values():
            out["devices"][d.name] = {"vendor": d.vendor, "rtsp_port": d.port, "channels": d.channels}
        out["devices"]["hik1"]["http_port"] = self.hik_api.port
        out["devices"]["dah1"]["http_port"] = self.dah_api.port
        return out


def start_simulator(workdir: Path, *, hik_channels: int = 2, dah_channels: int = 2, people_cameras: int = 1,
                    extra_devices: list[Any] | None = None) -> SimEnv:
    from tools.camsim.simulator import DEFAULT_PASSWORD, CameraSimulator, SimDevice
    from tools.mocks.dahua import DahuaChannel, DahuaMock
    from tools.mocks.hikvision import HikChannel, HikvisionMock
    from tools.mocks.server import MockHttpServer

    devices = [SimDevice("hik1", "hikvision", channels=hik_channels),
               SimDevice("dah1", "dahua", channels=dah_channels)]
    generic: list[str] = []
    if people_cameras:
        if not PEOPLE_VIDEO.is_file():
            raise RuntimeError(f"Falta el vídeo de personas {PEOPLE_VIDEO}: python -m tools.download_test_assets")
        for i in range(1, people_cameras + 1):
            # Cámara de puerta: vídeo real a 1280x720 (main) y 640x360 (sub), 15 fps
            devices.append(SimDevice(f"door{i}", "generic", channels=1, videos={1: PEOPLE_VIDEO},
                                     main_size=(1280, 720), sub_size=(640, 360), fps=15))
            generic.append(f"door{i}")
    devices += extra_devices or []
    sim = CameraSimulator(devices, workdir / "camsim").start(wait_ready=60)
    hik_names = ["Entrada", "Cajas", "Pasillo", "Almacén"] + [f"Canal {i}" for i in range(5, 65)]
    dah_names = ["Parking", "Muelle", "Oficina", "Pasillo 2"] + [f"Canal {i}" for i in range(5, 65)]
    hik = HikvisionMock(password=DEFAULT_PASSWORD,
                        channels=[HikChannel(hik_names[i], f"192.168.254.{i + 2}") for i in range(hik_channels)])
    dah = DahuaMock(password=DEFAULT_PASSWORD, channels=[DahuaChannel(dah_names[i]) for i in range(dah_channels)])
    hik_api = MockHttpServer(hik.app).start()
    dah_api = MockHttpServer(dah.app).start()
    return SimEnv(sim, hik_api, dah_api, hik_channels, dah_channels, generic)


# =========================================================================== API del backend
class BackendClient:
    """Cliente síncrono mínimo de la API del backend (login con cookie + CSRF)."""

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0) -> None:
        self.http = httpx.Client(base_url=base_url, headers=HEADERS, timeout=timeout)
        r = self.http.post("/api/auth/login", json={"username": username, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"Login fallido ({r.status_code}): {r.text[:200]}")

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, **kw: Any) -> Any:
        r = self.http.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any, expect: int = 201) -> Any:
        r = self.http.post(path, json=body)
        if r.status_code != expect:
            raise RuntimeError(f"POST {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def put(self, path: str, body: Any) -> Any:
        r = self.http.put(path, json=body)
        if r.status_code != 200:
            raise RuntimeError(f"PUT {path} → {r.status_code}: {r.text[:300]}")
        return r.json()


def seed_backend(api: BackendClient, simenv: SimEnv) -> dict[str, Any]:
    """Da de alta los equipos del simulador (importando canales) y reparte las cámaras en los muros."""
    from tools.camsim.simulator import DEFAULT_PASSWORD

    existing = {(d["host"], d["rtsp_port"]) for d in api.get("/api/devices")}
    sim = simenv.sim
    specs = [("NVR Hikvision (simulado)", "hikvision", "hik1", simenv.hik_api.port),
             ("NVR Dahua (simulado)", "dahua", "dah1", simenv.dah_api.port)]
    for name, vendor, dev, http_port in specs:
        if ("127.0.0.1", sim.device(dev).port) in existing:
            continue
        out = api.post("/api/devices", {"name": name, "vendor": vendor, "kind": "nvr", "host": "127.0.0.1",
                                        "http_port": http_port, "rtsp_port": sim.device(dev).port,
                                        "username": "admin", "password": DEFAULT_PASSWORD,
                                        "import_channels": "all"})
        if out.get("details", {}).get("import_error"):
            raise RuntimeError(f"No se importaron los canales de {name}: {out['details']['import_error']}")
    for i, dev in enumerate(simenv.generic, start=1):
        if ("127.0.0.1", sim.device(dev).port) in existing:
            continue
        d = api.post("/api/devices", {"name": f"Cámara puerta {i} (genérica)", "vendor": "generic",
                                      "kind": "camera", "host": "127.0.0.1", "http_port": free_port(),
                                      "rtsp_port": sim.device(dev).port, "username": "admin",
                                      "password": DEFAULT_PASSWORD})
        api.post("/api/cameras", {"device_id": d["id"], "channel": 1, "name": f"Puerta {i}",
                                  "main_path": "/ch1/main", "sub_path": "/ch1/sub"})
    cams = api.get("/api/cameras")
    ids = [c["id"] for c in cams]
    layouts = {1: 4, 2: 1, 3: 9, 4: 16}
    for monitor, grid in layouts.items():
        cells: list[str | None] = [None] * 16
        for k in range(min(grid, len(ids))):
            cells[k] = ids[(k + monitor - 1) % len(ids)] if grid < len(ids) else ids[k]
        api.put(f"/api/walls/{monitor}", {"grid": grid, "cells": cells})
    return {"cameras": [{"id": c["id"], "name": c["name"]} for c in cams]}


# =========================================================================== supervisor
@dataclass
class DevOptions:
    workdir: Path = DEFAULT_WORKDIR
    http_port: int = 8600
    sim: bool = False
    seed: bool = False
    pg: bool = False
    central: bool = False
    central_port: int = 8700
    analytics: bool = True
    env_file: Path | None = None


class DevStack:
    def __init__(self, opts: DevOptions) -> None:
        self.o = opts
        self.w = opts.workdir.resolve()
        self.logs = self.w / "logs"
        self.services: list[Service] = []
        self.pg: EmbeddedPostgres | None = None
        self.simenv: SimEnv | None = None
        self.env_file = opts.env_file or (self.w / "dev.env")
        self.state_file = self.w / "run.json"
        self.stop_file = self.w / "stop.request"

    # ------------------------------------------------------------------ preparación
    def prepare_env(self) -> dict[str, str]:
        values = read_env_file(self.env_file) if self.env_file.is_file() else {}
        if self.o.env_file is None:
            # .env de desarrollo propio: contraseñas aleatorias que solo existen en .tmp/dev/
            values.setdefault("VMS_DATA_DIR", str(self.w / "data"))
            values.setdefault("VMS_SITE_ID", "site-dev")
            values.setdefault("VMS_HTTP_HOST", "127.0.0.1")
            values["VMS_HTTP_PORT"] = str(self.o.http_port)
            values.setdefault("VMS_ADMIN_INITIAL_PASSWORD", "dev-" + secrets.token_urlsafe(9))
            values.setdefault("VMS_KIOSK_TOKEN", secrets.token_urlsafe(24))
            values.setdefault("VMS_CREDENTIAL_BACKEND", "file")
            values.setdefault("VMS_LLM_PROVIDER", "none")
            values.setdefault("VMS_ANALYTICS_MODELS_DIR", str(ROOT / "models"))
            values.setdefault("VMS_HEARTBEAT_SECONDS", "15")
            values.setdefault("VMS_CENTRAL_DATA_DIR", str(self.w / "central"))
            values.setdefault("VMS_CENTRAL_HTTP_HOST", "127.0.0.1")
            values["VMS_CENTRAL_HTTP_PORT"] = str(self.o.central_port)
            values.setdefault("VMS_CENTRAL_ADMIN_INITIAL_PASSWORD", "dev-" + secrets.token_urlsafe(9))
            write_env_file(self.env_file, values)
        return values

    def write_state(self, extra: dict[str, Any] | None = None) -> None:
        data = {"supervisor_pid": os.getpid(), "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "workdir": str(self.w), "env_file": str(self.env_file),
                "backend_url": f"http://127.0.0.1:{self.o.http_port}",
                "services": {s.name: s.pid for s in self.services}}
        if self.simenv:
            data["simulator"] = self.simenv.describe()
        if extra:
            data.update(extra)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)

    # ------------------------------------------------------------------ arranque
    def start(self) -> dict[str, Any]:
        self.w.mkdir(parents=True, exist_ok=True)
        self.stop_file.unlink(missing_ok=True)
        if self.state_file.is_file():
            old = json.loads(self.state_file.read_text(encoding="utf-8"))
            if pid_alive(int(old.get("supervisor_pid", 0))):
                raise RuntimeError(f"Ya hay un sistema de desarrollo en marcha (pid {old['supervisor_pid']}): "
                                   "«python -m tools.dev_run stop»")
        if port_in_use(self.o.http_port):
            raise RuntimeError(f"El puerto {self.o.http_port} ya está ocupado: usa --http-port")
        values = self.prepare_env()
        extra_env: dict[str, str] = {}
        if self.o.pg:
            self.pg = EmbeddedPostgres(self.w / "pgdata")
            extra_env["VMS_PG_DSN"] = self.pg.start()
        env = base_env(self.env_file, extra_env)
        if self.o.sim:
            log.info("Arrancando el simulador de cámaras...")
            self.simenv = start_simulator(self.w)

        backend = Service("backend", python_cmd("-m", "vms"), env, self.logs / "backend.out.log").start()
        self.services.append(backend)
        url = f"http://127.0.0.1:{self.o.http_port}"
        wait_until(lambda: httpx.get(f"{url}/api/health", timeout=2).status_code == 200, 60, "backend /api/health")
        log.info("Backend listo en %s", url)

        info: dict[str, Any] = {}
        if self.o.sim and self.o.seed:
            api = BackendClient(url, "admin", values.get("VMS_ADMIN_INITIAL_PASSWORD", ""))
            try:
                info["seed"] = seed_backend(api, self.simenv)  # type: ignore[arg-type]
            finally:
                api.close()
            log.info("Equipos simulados dados de alta y muros configurados")

        if self.o.analytics:
            models = Path(values.get("VMS_ANALYTICS_MODELS_DIR") or ROOT / "models")
            if (models / "rfdetr-nano.onnx").is_file():
                self.services.append(Service("analytics", python_cmd("-m", "analytics"), env,
                                             self.logs / "analytics.out.log").start())
            else:
                log.warning("Sin modelo en %s: la analítica no se arranca "
                            "(python -m analytics.tools.export_model)", models)
        if self.o.central:
            if not self.o.pg:
                log.warning("--central necesita --pg (lee PostgreSQL): no se arranca")
            else:
                if port_in_use(self.o.central_port):
                    raise RuntimeError(f"El puerto {self.o.central_port} del panel central está ocupado")
                self.services.append(Service("central", python_cmd("-m", "central"), env,
                                             self.logs / "central.out.log").start())
                curl = f"http://127.0.0.1:{self.o.central_port}"
                wait_until(lambda: httpx.get(f"{curl}/api/health", timeout=2).status_code == 200, 60,
                           "panel central /api/health")
                info["central_url"] = curl
        self.write_state(info)
        return info

    # ------------------------------------------------------------------ parada
    def stop(self) -> None:
        for svc in reversed(self.services):
            svc.stop()
        if self.simenv is not None:
            self.simenv.stop()
            self.simenv = None
        if self.pg is not None:
            self.pg.stop()
            self.pg = None
        check_orphan_mediamtx(self.w / "data")
        self.state_file.unlink(missing_ok=True)
        self.stop_file.unlink(missing_ok=True)

    def supervise(self) -> None:
        """Bucle del supervisor: avisa si un servicio muere y atiende la petición de parada."""
        reported: set[str] = set()
        while not self.stop_file.exists():
            for svc in self.services:
                if not svc.alive() and svc.name not in reported:
                    reported.add(svc.name)
                    log.error("%s terminó inesperadamente (código %s); mira %s", svc.name,
                              svc.proc.returncode if svc.proc else "?", svc.log_file)
            time.sleep(STOP_POLL_S)
        log.info("Parada solicitada")


def check_orphan_mediamtx(data_dir: Path) -> None:
    pid_file = data_dir / "mediamtx" / "mediamtx.pid"
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").split()[0])
    except (ValueError, IndexError, OSError):
        return
    if pid_alive(pid):
        log.error("MediaMTX (pid %d) sigue vivo tras parar el backend: se detiene", pid)
        terminate_pid(pid, 5, "mediamtx")


# =========================================================================== órdenes
def _load_state(workdir: Path) -> dict[str, Any] | None:
    f = workdir / "run.json"
    if not f.is_file():
        return None
    try:
        data: dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        return None


def cmd_start(args: argparse.Namespace) -> int:
    opts = DevOptions(workdir=args.workdir, http_port=args.http_port, sim=args.sim or args.seed, seed=args.seed,
                      pg=args.pg or args.central, central=args.central, central_port=args.central_port,
                      analytics=not args.no_analytics, env_file=args.env_file)
    if args.detach:
        argv = [a for a in sys.argv[1:] if a != "--detach"]
        logf = opts.workdir / "logs" / "dev_run.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        with open(logf, "ab") as fh:
            proc = subprocess.Popen([sys.executable, "-m", "tools.dev_run", *argv], cwd=ROOT, stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, **_popen_group_kwargs())
        try:
            wait_until(lambda: (_load_state(opts.workdir) or {}).get("supervisor_pid") == proc.pid
                       or proc.poll() is not None, 240, "arranque del sistema")
        except TimeoutError:
            print(f"El sistema no terminó de arrancar; mira {logf}", file=sys.stderr)
            return 1
        if proc.poll() is not None:
            print(f"El arranque falló (código {proc.returncode}); mira {logf}", file=sys.stderr)
            return 1
        _print_summary(opts.workdir)
        return 0

    stack = DevStack(opts)
    stop_event = threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        stop_event.set()
        stack.stop_file.touch()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)
    try:
        stack.start()
        _print_summary(opts.workdir)
        print("Sistema en marcha. Para pararlo: Ctrl+C aquí o «python -m tools.dev_run stop».", flush=True)
        stack.supervise()
        return 0
    except Exception as exc:  # noqa: BLE001 - se informa y se para lo que hubiera arrancado
        log.error("No se pudo arrancar el sistema: %s", exc)
        return 1
    finally:
        stack.stop()
        print("Sistema parado.", flush=True)


def _print_summary(workdir: Path) -> None:
    st = _load_state(workdir) or {}
    env = read_env_file(Path(st.get("env_file", workdir / "dev.env")))
    print(f"\nBackend:     {st.get('backend_url')}  (usuario admin / {env.get('VMS_ADMIN_INITIAL_PASSWORD', '?')})")
    kiosk = env.get("VMS_KIOSK_TOKEN")
    if kiosk:
        print(f"Muro kiosco: {st.get('backend_url')}/api/auth/kiosk?token={kiosk}&next=/wall/1")
    if st.get("central_url"):
        central_pw = env.get("VMS_CENTRAL_ADMIN_INITIAL_PASSWORD", "?")
        print(f"Central:     {st['central_url']}  (usuario admin / {central_pw})")
    for name, pid in (st.get("services") or {}).items():
        print(f"  {name:10} pid {pid}")
    print(f"Registros:   {workdir / 'logs'} y {Path(env.get('VMS_DATA_DIR', workdir / 'data')) / 'logs'}\n", flush=True)


def cmd_status(args: argparse.Namespace) -> int:
    st = _load_state(args.workdir)
    if not st or not pid_alive(int(st.get("supervisor_pid", 0))):
        print("No hay ningún sistema de desarrollo en marcha.")
        return 1
    print(f"Supervisor pid {st['supervisor_pid']} (desde {st.get('started_at')})")
    for name, pid in (st.get("services") or {}).items():
        print(f"  {name:10} pid {pid:<7} {'vivo' if pid_alive(pid) else 'PARADO'}")
    try:
        h = httpx.get(f"{st['backend_url']}/api/health", timeout=3).json()
        print(f"Backend: {h.get('status')} · motor {'en marcha' if h.get('engine', {}).get('running') else 'parado'}")
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Backend sin respuesta: {exc}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    st = _load_state(args.workdir)
    if not st:
        print("No hay ningún sistema de desarrollo en marcha.")
        return 0
    sup = int(st.get("supervisor_pid", 0))
    if pid_alive(sup):
        (args.workdir / "stop.request").touch()
        try:
            wait_until(lambda: not pid_alive(sup), 90, "parada del supervisor", 0.5)
            print("Sistema parado.")
            return 0
        except TimeoutError:
            print("El supervisor no respondió: se paran los procesos uno a uno", file=sys.stderr)
    for name, pid in reversed(list((st.get("services") or {}).items())):
        if pid_alive(pid):
            terminate_pid(pid, 20, name)
            print(f"{name} parado")
    if pid_alive(sup):
        terminate_pid(sup, 10, "supervisor")
    data_dir = Path(read_env_file(Path(st.get("env_file", ""))).get("VMS_DATA_DIR", args.workdir / "data"))
    check_orphan_mediamtx(data_dir)
    (args.workdir / "run.json").unlink(missing_ok=True)
    (args.workdir / "stop.request").unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.dev_run", description="Sistema completo en desarrollo")
    p.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR, help="carpeta de trabajo (por defecto .tmp/dev)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="arranca el sistema")
    s.add_argument("--sim", action="store_true", help="simulador de cámaras (2 NVR + cámara de puerta con personas)")
    s.add_argument("--seed", action="store_true", help="da de alta los equipos simulados y configura los muros")
    s.add_argument("--pg", action="store_true", help="PostgreSQL embebido (pgserver) con las migraciones")
    s.add_argument("--central", action="store_true", help="panel central (implica --pg)")
    s.add_argument("--no-analytics", action="store_true", help="no arrancar la analítica")
    s.add_argument("--http-port", type=int, default=8600)
    s.add_argument("--central-port", type=int, default=8700)
    s.add_argument("--env-file", type=Path, help="usar este .env en vez del de desarrollo")
    s.add_argument("--detach", action="store_true", help="arrancar en segundo plano y volver")
    sub.add_parser("status", help="estado de los procesos")
    sub.add_parser("stop", help="para todo de forma ordenada")
    args = p.parse_args(argv)
    args.workdir = Path(args.workdir).resolve()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return {"start": cmd_start, "status": cmd_status, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
