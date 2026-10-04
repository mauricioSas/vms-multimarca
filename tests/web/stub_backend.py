"""Backend de pruebas para la interfaz web: implementa la API de docs/CONTRATO.md §6 de forma
compacta sobre las piezas compartidas (ConfigRepository, CredentialStore, modelos) y un `Engine`
cualquiera (FakeEngine o MtxTestEngine con MediaMTX real).

No es el backend del producto (ese es vms.api). Existe para probar la interfaz en el
navegador de forma aislada y para enseñarla sin cámaras:

    .venv/bin/python -m tests.web.stub_backend --camsim      # simulador + MediaMTX + vídeo real
    .venv/bin/python -m tests.web.stub_backend               # sin vídeo (FakeEngine)

Usuarios de prueba: admin / admin-pass-1234 (administrador) y operador / operador-pass-1234.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Body, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import TypeAdapter, ValidationError

from vms import __version__
from vms.core.config_store import ConfigRepository, ConfigStore
from vms.core.credentials import CredentialStore, EncryptedFileBackend
from vms.core.errors import (AuthError, ConflictError, EngineUnavailable, ForbiddenError, NotFoundError,
                             RateLimited, ValidationFailed, VmsError)
from vms.core.interfaces import (ChannelInfo, DeviceClient, DeviceTestResult, DiscoveredDevice, Engine,
                                 PathStatus)
from vms.core.models import (AnalyticsRule, AppConfig, Camera, CameraAnalytics, CameraCreate, CameraUpdate,
                             Device, DeviceCreate, DeviceTestRequest, DeviceUpdate, LineRule,
                             RetentionSettings, SystemSettings, WallUpdate, ZoneRule, utcnow)
from vms.core.naming import mtx_path
from vms.core.paths import AppPaths
from vms.core.sources import build_camera_sources
from vms.web import mount_web

log = logging.getLogger("tests.web.stub")

DEFAULT_USERS = {"admin": ("admin-pass-1234", "admin"), "operador": ("operador-pass-1234", "operator")}
ROLE_LEVEL = {"kiosk": 0, "operator": 1, "admin": 2}
RuleAdapter: TypeAdapter[LineRule | ZoneRule] = TypeAdapter(AnalyticsRule)  # type: ignore[arg-type]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def dump(model: Any) -> Any:
    return json.loads(model.model_dump_json())


@dataclass
class Session:
    username: str
    role: Literal["admin", "operator"]
    kiosk: bool
    expires: float


@dataclass
class StubOptions:
    users: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DEFAULT_USERS))
    kiosk_token: str | None = "kiosk-token-pruebas"
    status_interval: float = 5.0
    ping_interval: float = 15.0
    # fábrica de clientes de equipos: (device, password) -> DeviceClient
    device_client: Callable[[Device | DeviceTestRequest, str], DeviceClient] | None = None
    discover: Callable[[float], Awaitable[list[DiscoveredDevice]]] | None = None
    # snapshot alternativo (p. ej. un frame del RTSP local); en memoria, nunca a disco
    snapshot: Callable[[str, str], Awaitable[bytes]] | None = None
    analytics_status: Callable[[], dict[str, Any]] | None = None


class StubBackend:
    def __init__(self, data_dir: Path, engine: Engine, options: StubOptions | None = None) -> None:
        self.paths = AppPaths(Path(data_dir)).ensure()
        self.engine = engine
        self.opt = options or StubOptions()
        self.repo = ConfigRepository(ConfigStore(self.paths.config_file))
        key = EncryptedFileBackend.load_or_create_key(self.paths.secrets_dir)
        self.creds = CredentialStore(EncryptedFileBackend(self.paths.secrets_dir / "credentials.enc", key))
        self.sessions: dict[str, Session] = {}
        self.failures: dict[str, list[float]] = {}
        self.listeners: set[asyncio.Queue[str]] = set()
        self.started = time.monotonic()
        self.snapshot_last: dict[str, float] = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5))
        self.app = self._build()

    # ================================================================== utilidades
    async def _apply_engine(self, cfg: AppConfig) -> None:
        try:
            await self.engine.apply(build_camera_sources(cfg, self.creds), cfg.settings.recording,
                                    cfg.settings.retention, str(self.paths.recordings_dir))
        except VmsError as exc:
            log.warning("No se pudo aplicar la configuración al motor: %s", exc.message)

    async def _update(self, scope: str, fn: Callable[[AppConfig], Any]) -> Any:
        try:
            result = await self.repo.update(fn)
        except ValidationError as exc:
            raise ValidationFailed("Datos no válidos", details={"fields": [
                {"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}) from exc
        self._broadcast("config", {"revision": self.repo.revision, "scope": scope})
        return result

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        for q in list(self.listeners):
            if q.qsize() < 100:
                q.put_nowait(msg)

    def _session(self, request: Request, level: str = "kiosk") -> Session:
        token = request.cookies.get("vms_session", "")
        s = self.sessions.get(token)
        if s is None or s.expires < time.time():
            raise AuthError("Inicia sesión para continuar")
        effective = "kiosk" if s.kiosk else s.role
        if ROLE_LEVEL[effective] < ROLE_LEVEL[level]:
            raise ForbiddenError("No tienes permiso para esta acción")
        return s

    async def _paths(self) -> dict[str, PathStatus]:
        try:
            return await self.engine.paths_status()
        except VmsError:
            return {}

    def _live(self, cam: Camera, paths: dict[str, PathStatus]) -> dict[str, Any]:
        main = paths.get(mtx_path(cam.id, "main"))
        sub = paths.get(mtx_path(cam.id, "sub")) if cam.has_sub else None
        warn = None
        tracks = (sub.tracks if sub and sub.tracks else (main.tracks if main else []))
        if any("265" in t or "HEVC" in t.upper() for t in tracks):
            warn = ("El subflujo es H.265: el navegador no lo reproduce por WebRTC. "
                    "Cámbialo a H.264 en el NVR")
        return {"online": bool(main and main.ready), "recording": bool(main and main.recording),
                "readers": (main.readers if main else 0) + (sub.readers if sub else 0),
                "tracks": tracks, "codec_warning": warn}

    def _camera_out(self, cfg: AppConfig, cam: Camera, paths: dict[str, PathStatus]) -> dict[str, Any]:
        dev = cfg.device(cam.device_id)
        return {**dump(cam), "device_name": dev.name if dev else "", "vendor": dev.vendor if dev else "",
                "live": self._live(cam, paths)}

    def _device_out(self, cfg: AppConfig, dev: Device, paths: dict[str, PathStatus]) -> dict[str, Any]:
        cams = cfg.cameras_of(dev.id)
        online: bool | None = None
        if cams and paths:
            online = any(self._live(c, paths)["online"] for c in cams)
        return {**dump(dev), "has_password": self.creds.has_device_password(dev.id),
                "cameras": [c.id for c in cams], "online": online}

    def _client(self, dev: Device | DeviceTestRequest, password: str) -> DeviceClient:
        if self.opt.device_client is None:
            from tests.fakes import FakeDeviceClient
            return FakeDeviceClient(dev.vendor, channels=4 if dev.kind == "nvr" else 1, kind=dev.kind)  # type: ignore[return-value]
        return self.opt.device_client(dev, password)

    async def _test(self, dev: Device | DeviceTestRequest, password: str) -> DeviceTestResult:
        client = self._client(dev, password)
        try:
            info = await client.probe()
            chans = await client.list_channels()
            return DeviceTestResult(ok=True, reachable=True, auth_ok=True, rtsp_ok=True, info=info, channels=chans,
                                    message=f"{info.model or 'Equipo'} con {len(chans)} canales")
        except VmsError as exc:
            auth = False if exc.code == "device_auth_failed" else None
            return DeviceTestResult(ok=False, reachable=exc.code != "device_unreachable", auth_ok=auth,
                                    message=exc.message)
        finally:
            await client.aclose()

    def _cameras_from_channels(self, cfg: AppConfig, dev: Device, chans: list[ChannelInfo],
                               wanted: list[int] | Literal["all"]) -> list[Camera]:
        existing = {c.channel for c in cfg.cameras_of(dev.id)}
        out = []
        for ch in chans:
            if wanted != "all" and ch.channel not in wanted:
                continue
            if ch.channel in existing:
                continue
            out.append(Camera(name=(ch.name or f"{dev.name} {ch.channel}")[:80], device_id=dev.id,
                              channel=ch.channel, has_sub=ch.has_sub, main_path=ch.main_path, sub_path=ch.sub_path))
        return out

    # ================================================================== aplicación
    def _build(self) -> FastAPI:
        backend = self

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            await backend.engine.start()
            await backend._apply_engine(backend.repo.snapshot())
            backend.repo.subscribe(backend._apply_engine)
            task = asyncio.create_task(backend._status_loop())
            try:
                yield
            finally:
                task.cancel()
                await backend.http.aclose()

        app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

        @app.exception_handler(VmsError)
        async def vms_error(_: Request, exc: VmsError) -> JSONResponse:
            headers = {}
            if isinstance(exc, RateLimited) and "retry_after" in exc.details:
                headers["Retry-After"] = str(exc.details["retry_after"])
            return JSONResponse(exc.to_dict(), status_code=exc.status, headers=headers)

        @app.exception_handler(RequestValidationError)
        async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
            fields = [{"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))} for e in exc.errors()]
            return JSONResponse({"error": {"code": "validation_error", "message": "Datos no válidos",
                                           "details": {"fields": fields}}}, status_code=422)

        @app.middleware("http")
        async def csrf(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
                if request.headers.get("X-Requested-With") != "vms":
                    return JSONResponse({"error": {"code": "csrf", "message": "Petición rechazada (CSRF)",
                                                   "details": {}}}, status_code=403)
            return await call_next(request)

        self._routes_auth(app)
        self._routes_devices(app)
        self._routes_cameras(app)
        self._routes_live(app)
        self._routes_recordings(app)
        self._routes_misc(app)
        mount_web(app)
        return app

    # ------------------------------------------------------------------ auth
    def _routes_auth(self, app: FastAPI) -> None:
        b = self

        def new_session(response: Response, username: str, role: str, kiosk: bool) -> None:
            token = secrets.token_urlsafe(32)
            b.sessions[token] = Session(username, role, kiosk, time.time() + 12 * 3600)  # type: ignore[arg-type]
            response.set_cookie("vms_session", token, httponly=True, samesite="strict", path="/")

        @app.post("/api/auth/login")
        async def login(request: Request, response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", ""))
            key = f"{request.client.host if request.client else '?'}|{username.lower()}"
            now = time.time()
            fails = [t for t in b.failures.get(key, []) if now - t < 300]
            if len(fails) >= 5:
                raise RateLimited("Demasiados intentos fallidos", details={"retry_after": int(300 - (now - fails[0]))})
            user = next(((u, v) for u, v in b.opt.users.items() if u.lower() == username.lower()), None)
            if user is None or not secrets.compare_digest(user[1][0].encode(), password.encode()):
                fails.append(now)
                b.failures[key] = fails
                raise AuthError("Usuario o contraseña incorrectos", code="invalid_credentials")
            b.failures.pop(key, None)
            new_session(response, user[0], user[1][1], False)
            return {"user": {"username": user[0], "role": user[1][1], "enabled": True,
                             "created_at": iso(utcnow()), "last_login_at": iso(utcnow())}}

        @app.post("/api/auth/logout", status_code=204)
        async def logout(request: Request) -> Response:
            b._session(request)
            b.sessions.pop(request.cookies.get("vms_session", ""), None)
            resp = Response(status_code=204)
            resp.delete_cookie("vms_session", path="/")
            return resp

        @app.get("/api/auth/me")
        async def me(request: Request) -> dict[str, Any]:
            s = b._session(request)
            return {"username": s.username, "role": s.role, "kiosk": s.kiosk}

        @app.get("/api/auth/setup")
        async def setup_needed() -> dict[str, Any]:
            return {"needed": not b.opt.users}

        @app.post("/api/auth/setup", status_code=201)
        async def setup(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            if b.opt.users:
                raise ConflictError("Ya hay usuarios")
            if not request.client or request.client.host not in ("127.0.0.1", "::1"):
                raise ForbiddenError("Solo desde el propio equipo")
            b.opt.users[str(body["username"])] = (str(body["password"]), "admin")
            return {"username": body["username"], "role": "admin", "enabled": True}

        @app.get("/api/auth/kiosk")
        async def kiosk(token: str = "", next: str = "/wall/1") -> Response:
            if not b.opt.kiosk_token or not secrets.compare_digest(token.encode(), b.opt.kiosk_token.encode()):
                raise AuthError("Token de kiosco no válido")
            if not next.startswith("/") or next.startswith("//"):
                next = "/wall/1"
            resp = RedirectResponse(next, status_code=303)
            new_session(resp, "kiosco", "operator", True)
            return resp

    # ------------------------------------------------------------------ equipos
    def _routes_devices(self, app: FastAPI) -> None:
        b = self

        @app.get("/api/devices")
        async def list_devices(request: Request) -> list[dict[str, Any]]:
            b._session(request, "operator")
            cfg, paths = b.repo.snapshot(), await b._paths()
            return [b._device_out(cfg, d, paths) for d in cfg.devices]

        @app.post("/api/devices", status_code=201)
        async def create_device(request: Request, body: DeviceCreate) -> dict[str, Any]:
            b._session(request, "admin")
            dev = Device(**body.model_dump(exclude={"password", "import_channels"}))
            pw = body.password.get_secret_value() if body.password else ""
            if pw:
                b.creds.set_device_password(dev.id, pw)
            import_error = None
            new_cams: list[Camera] = []
            if body.import_channels:
                try:
                    client = b._client(dev, pw)
                    try:
                        info = await client.probe()
                        dev.model, dev.serial, dev.firmware = info.model, info.serial, info.firmware
                        chans = await client.list_channels()
                    finally:
                        await client.aclose()
                    new_cams = b._cameras_from_channels(AppConfig(devices=[dev]), dev, chans, body.import_channels)
                except VmsError as exc:
                    import_error = exc.message

            def mutate(cfg: AppConfig) -> None:
                cfg.devices.append(dev)
                cfg.cameras.extend(new_cams)

            await b._update("devices", mutate)
            cfg, paths = b.repo.snapshot(), await b._paths()
            out = b._device_out(cfg, dev, paths)
            if import_error:
                out["details"] = {"import_error": import_error}
            return out

        @app.post("/api/devices/test")
        async def test_new(request: Request, body: DeviceTestRequest) -> dict[str, Any]:
            b._session(request, "admin")
            return dump(await b._test(body, body.password.get_secret_value() if body.password else ""))

        @app.get("/api/devices/{device_id}")
        async def get_device(request: Request, device_id: str) -> dict[str, Any]:
            b._session(request, "operator")
            cfg = b.repo.snapshot()
            dev = cfg.device(device_id)
            if not dev:
                raise NotFoundError("Equipo no encontrado")
            return b._device_out(cfg, dev, await b._paths())

        @app.patch("/api/devices/{device_id}")
        async def patch_device(request: Request, device_id: str, body: DeviceUpdate) -> dict[str, Any]:
            b._session(request, "admin")
            changes = body.model_dump(exclude_unset=True, exclude={"password"})
            changes = {k: v for k, v in changes.items() if v is not None}

            def mutate(cfg: AppConfig) -> None:
                dev = cfg.device(device_id)
                if not dev:
                    raise NotFoundError("Equipo no encontrado")
                for k, v in changes.items():
                    setattr(dev, k, v)
                dev.updated_at = utcnow()

            if body.password is not None:
                b.creds.set_device_password(device_id, body.password.get_secret_value())
            await b._update("devices", mutate)
            cfg = b.repo.snapshot()
            return b._device_out(cfg, cfg.device(device_id), await b._paths())  # type: ignore[arg-type]

        @app.delete("/api/devices/{device_id}", status_code=204)
        async def delete_device(request: Request, device_id: str) -> Response:
            b._session(request, "admin")

            def mutate(cfg: AppConfig) -> None:
                if not cfg.device(device_id):
                    raise NotFoundError("Equipo no encontrado")
                cfg.remove_device(device_id)

            await b._update("devices", mutate)
            b.creds.delete_device_password(device_id)
            return Response(status_code=204)

        @app.post("/api/devices/{device_id}/test")
        async def test_saved(request: Request, device_id: str) -> dict[str, Any]:
            b._session(request, "admin")
            dev = b.repo.snapshot().device(device_id)
            if not dev:
                raise NotFoundError("Equipo no encontrado")
            return dump(await b._test(dev, b.creds.get_device_password(device_id)))

        @app.get("/api/devices/{device_id}/channels")
        async def channels(request: Request, device_id: str) -> list[dict[str, Any]]:
            b._session(request, "admin")
            dev = b.repo.snapshot().device(device_id)
            if not dev:
                raise NotFoundError("Equipo no encontrado")
            client = b._client(dev, b.creds.get_device_password(device_id))
            try:
                return [dump(c) for c in await client.list_channels()]
            finally:
                await client.aclose()

        @app.post("/api/devices/{device_id}/channels/import", status_code=201)
        async def import_channels(request: Request, device_id: str,
                                  body: dict[str, Any] = Body(...)) -> list[dict[str, Any]]:
            b._session(request, "admin")
            cfg = b.repo.snapshot()
            dev = cfg.device(device_id)
            if not dev:
                raise NotFoundError("Equipo no encontrado")
            client = b._client(dev, b.creds.get_device_password(device_id))
            try:
                chans = await client.list_channels()
            finally:
                await client.aclose()
            wanted = body.get("channels", "all")
            new_cams = b._cameras_from_channels(cfg, dev, chans, wanted if wanted == "all" else [int(x) for x in wanted])
            await b._update("cameras", lambda c: c.cameras.extend(new_cams))
            return [dump(c) for c in new_cams]

        @app.post("/api/discovery/scan")
        async def scan(request: Request, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
            b._session(request, "admin")
            timeout = max(1.0, min(10.0, float(body.get("timeout_s", 3))))
            found = await b.opt.discover(timeout) if b.opt.discover else []
            hosts = {d.host for d in b.repo.snapshot().devices}
            return {"devices": [{**dump(d), "already_added": d.host in hosts} for d in found]}

    # ------------------------------------------------------------------ cámaras, muros
    def _routes_cameras(self, app: FastAPI) -> None:
        b = self

        @app.get("/api/cameras")
        async def list_cameras(request: Request) -> list[dict[str, Any]]:
            b._session(request)
            cfg, paths = b.repo.snapshot(), await b._paths()
            return [b._camera_out(cfg, c, paths) for c in cfg.cameras]

        @app.post("/api/cameras", status_code=201)
        async def create_camera(request: Request, body: CameraCreate) -> dict[str, Any]:
            b._session(request, "admin")
            cam = Camera(**body.model_dump())

            def mutate(cfg: AppConfig) -> None:
                if not cfg.device(cam.device_id):
                    raise NotFoundError("Equipo no encontrado")
                if any(c.channel == cam.channel for c in cfg.cameras_of(cam.device_id)):
                    raise ConflictError("Ese canal de ese equipo ya está dado de alta")
                cfg.cameras.append(cam)

            await b._update("cameras", mutate)
            return b._camera_out(b.repo.snapshot(), cam, await b._paths())

        @app.get("/api/cameras/{camera_id}")
        async def get_camera(request: Request, camera_id: str) -> dict[str, Any]:
            b._session(request)
            cfg = b.repo.snapshot()
            cam = cfg.camera(camera_id)
            if not cam:
                raise NotFoundError("Cámara no encontrada")
            return b._camera_out(cfg, cam, await b._paths())

        @app.patch("/api/cameras/{camera_id}")
        async def patch_camera(request: Request, camera_id: str, body: CameraUpdate) -> dict[str, Any]:
            b._session(request, "admin")
            changes = body.model_dump(exclude_unset=True)

            def mutate(cfg: AppConfig) -> None:
                cam = cfg.camera(camera_id)
                if not cam:
                    raise NotFoundError("Cámara no encontrada")
                for k, v in changes.items():
                    if v is None and k not in ("main_path", "sub_path"):
                        continue
                    setattr(cam, k, v)
                cam.updated_at = utcnow()

            await b._update("cameras", mutate)
            cfg = b.repo.snapshot()
            return b._camera_out(cfg, cfg.camera(camera_id), await b._paths())  # type: ignore[arg-type]

        @app.delete("/api/cameras/{camera_id}", status_code=204)
        async def delete_camera(request: Request, camera_id: str) -> Response:
            b._session(request, "admin")

            def mutate(cfg: AppConfig) -> None:
                if not cfg.camera(camera_id):
                    raise NotFoundError("Cámara no encontrada")
                cfg.remove_camera(camera_id)

            await b._update("cameras", mutate)
            return Response(status_code=204)

        @app.get("/api/cameras/{camera_id}/snapshot")
        async def snapshot(request: Request, camera_id: str, stream: Literal["main", "sub"] = "sub") -> Response:
            b._session(request, "admin")
            cfg = b.repo.snapshot()
            cam = cfg.camera(camera_id)
            if not cam:
                raise NotFoundError("Cámara no encontrada")
            now = time.monotonic()
            if now - b.snapshot_last.get(camera_id, 0) < 1.0:
                raise RateLimited("Espera un segundo entre capturas", details={"retry_after": 1})
            b.snapshot_last[camera_id] = now
            if b.opt.snapshot:
                data = await b.opt.snapshot(camera_id, stream)
            else:
                dev = cfg.device(cam.device_id)
                client = b._client(dev, b.creds.get_device_password(dev.id))  # type: ignore[arg-type]
                try:
                    data = await client.snapshot(cam.channel, stream)
                finally:
                    await client.aclose()
            return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        @app.get("/api/walls")
        async def walls(request: Request) -> list[dict[str, Any]]:
            b._session(request)
            return [dump(w) for w in b.repo.snapshot().walls]

        @app.get("/api/walls/{monitor}")
        async def wall(request: Request, monitor: int) -> Response:
            b._session(request)
            w = b.repo.snapshot().wall(monitor)
            if not w:
                raise NotFoundError("Monitor no encontrado")
            return JSONResponse(dump(w), headers={"ETag": f'"{b.repo.revision}"'})

        @app.put("/api/walls/{monitor}")
        async def put_wall(request: Request, monitor: int, body: WallUpdate) -> dict[str, Any]:
            s = b._session(request, "operator")
            if s.kiosk:
                raise ForbiddenError("El kiosco es de solo lectura")

            def mutate(cfg: AppConfig) -> None:
                w = cfg.wall(monitor)
                if not w:
                    raise NotFoundError("Monitor no encontrado")
                if body.cells is not None:
                    ids = {c.id for c in cfg.cameras}
                    bad = [c for c in body.cells if c and c not in ids]
                    if bad:
                        raise ValidationFailed("Hay cámaras que no existen",
                                               details={"fields": [{"loc": ["cells"], "msg": f"No existe: {bad[0]}"}]})
                    w.cells = list(body.cells)
                if body.grid is not None:
                    w.grid = body.grid
                if body.name is not None:
                    w.name = body.name

            await b._update("walls", mutate)
            return dump(b.repo.snapshot().wall(monitor))

    # ------------------------------------------------------------------ vivo (proxy WHEP)
    def _routes_live(self, app: FastAPI) -> None:
        b = self

        def target(camera_id: str, stream: str) -> str:
            cfg = b.repo.snapshot()
            cam = cfg.camera(camera_id)
            if not cam or stream not in ("main", "sub"):
                raise NotFoundError("Cámara no encontrada")
            if stream == "sub" and not cam.has_sub:
                stream = "main"
            return b.engine.whep_url(camera_id, stream)  # type: ignore[arg-type]

        @app.get("/api/live/{camera_id}")
        async def live_info(request: Request, camera_id: str, stream: Literal["main", "sub"] = "sub") -> dict[str, Any]:
            b._session(request)
            target(camera_id, stream)
            p = (await b._paths()).get(mtx_path(camera_id, stream))
            return {"camera_id": camera_id, "stream": stream, "whep_url": f"/api/live/{camera_id}/{stream}/whep",
                    "ready": bool(p and p.ready), "tracks": p.tracks if p else []}

        @app.options("/api/live/{camera_id}/{stream}/whep")
        async def whep_options(request: Request, camera_id: str, stream: str) -> Response:
            b._session(request)
            try:
                r = await b.http.options(target(camera_id, stream))
            except httpx.HTTPError as exc:
                raise EngineUnavailable("El motor de vídeo no responde") from exc
            headers = {k: v for k, v in r.headers.items() if k.lower() in ("link", "accept-patch")}
            return Response(status_code=204, headers=headers)

        @app.post("/api/live/{camera_id}/{stream}/whep")
        async def whep_offer(request: Request, camera_id: str, stream: str) -> Response:
            b._session(request)
            url = target(camera_id, stream)
            offer = await request.body()
            try:
                r = await b.http.post(url, content=offer, headers={"Content-Type": "application/sdp"})
            except httpx.HTTPError as exc:
                raise EngineUnavailable("El motor de vídeo no responde") from exc
            if r.status_code == 404:
                raise NotFoundError("La cámara no envía vídeo ahora mismo", code="stream_not_ready")
            if r.status_code != 201:
                raise EngineUnavailable(f"El motor rechazó la conexión ({r.status_code})")
            session = r.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
            headers = {"Location": f"/api/live/{camera_id}/{stream}/whep/{session}"}
            for k in ("ETag", "Accept-Patch", "Link"):
                if k in r.headers:
                    headers[k] = r.headers[k]
            return Response(r.content, status_code=201, media_type="application/sdp", headers=headers)

        @app.patch("/api/live/{camera_id}/{stream}/whep/{session}")
        async def whep_patch(request: Request, camera_id: str, stream: str, session: str) -> Response:
            b._session(request)
            r = await b.http.patch(f"{target(camera_id, stream)}/{session}", content=await request.body(),
                                   headers={"Content-Type": "application/trickle-ice-sdpfrag"})
            return Response(status_code=204 if r.status_code < 300 else r.status_code)

        @app.delete("/api/live/{camera_id}/{stream}/whep/{session}")
        async def whep_delete(request: Request, camera_id: str, stream: str, session: str) -> Response:
            b._session(request)
            try:
                await b.http.delete(f"{target(camera_id, stream)}/{session}")
            except httpx.HTTPError as exc:
                log.warning("DELETE WHEP falló: %s", exc)
            return Response(status_code=200)

    # ------------------------------------------------------------------ grabaciones
    def _routes_recordings(self, app: FastAPI) -> None:
        b = self

        @app.get("/api/recordings/summary")
        async def summary(request: Request) -> list[dict[str, Any]]:
            b._session(request, "operator")
            out = []
            for cam in b.repo.snapshot().cameras:
                spans = await b.engine.list_recordings(cam.id, None, None)
                if spans:
                    out.append({"camera_id": cam.id, "first": iso(spans[0].start), "last": iso(spans[-1].end),
                                "bytes": 0})
            return out

        @app.get("/api/recordings/{camera_id}/timeline")
        async def timeline(request: Request, camera_id: str, start: datetime | None = None,
                           end: datetime | None = None) -> dict[str, Any]:
            b._session(request, "operator")
            if not b.repo.snapshot().camera(camera_id):
                raise NotFoundError("Cámara no encontrada")
            spans = await b.engine.list_recordings(camera_id, start, end)
            return {"camera_id": camera_id, "spans": [
                {"start": iso(s.start), "end": iso(s.end), "duration": s.duration} for s in spans]}

        @app.get("/api/recordings/{camera_id}/video")
        async def video(request: Request, camera_id: str, start: datetime,
                        duration: float = Query(60, gt=0, le=3600),
                        format: Literal["fmp4", "mp4"] = "fmp4", download: int = 0) -> Response:
            b._session(request, "operator")
            cam = b.repo.snapshot().camera(camera_id)
            if not cam:
                raise NotFoundError("Cámara no encontrada")
            url = b.engine.playback_get_url(camera_id, start, duration, format)
            req = b.http.build_request("GET", url, timeout=httpx.Timeout(None, connect=5))
            try:
                r = await b.http.send(req, stream=True)
            except httpx.HTTPError as exc:
                raise EngineUnavailable("El servidor de reproducción no responde") from exc
            if r.status_code != 200:
                await r.aclose()
                if r.status_code == 404:
                    raise NotFoundError("No hay grabación en ese momento", code="no_recording")
                raise EngineUnavailable(f"Reproducción: respuesta {r.status_code}")
            headers = {"Cache-Control": "no-store"}
            if download:
                safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cam.name)[:40] or camera_id
                headers["Content-Disposition"] = (
                    f'attachment; filename="{safe}_{start.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")}.mp4"')

            async def body() -> AsyncIterator[bytes]:
                try:
                    async for chunk in r.aiter_raw():
                        yield chunk
                finally:
                    await r.aclose()

            return StreamingResponse(body(), media_type="video/mp4", headers=headers)

    # ------------------------------------------------------------------ estado, ajustes, analítica, SSE
    def _routes_misc(self, app: FastAPI) -> None:
        b = self

        @app.get("/api/health")
        async def health() -> dict[str, Any]:
            st = await b.engine.status()
            return {"status": "ok" if st.running else "down", "version": __version__,
                    "uptime_s": round(time.monotonic() - b.started, 1),
                    "engine": {"running": st.running, "api_ok": st.api_ok}}

        @app.get("/api/status")
        async def status(request: Request) -> dict[str, Any]:
            b._session(request, "operator")
            cfg, paths = b.repo.snapshot(), await b._paths()
            st = await b.engine.status()
            disk = await b.engine.disk_usage()
            cams = []
            for c in cfg.cameras:
                live = b._live(c, paths)
                main = paths.get(mtx_path(c.id, "main"))
                cams.append({"camera_id": c.id, "name": c.name, "online": live["online"],
                             "recording": live["recording"], "readers": live["readers"],
                             "bytes_received": main.bytes_received if main else 0,
                             "last_error": main.last_error if main else ""})
            analytics = b.opt.analytics_status() if b.opt.analytics_status else {"running": False}
            return {"engine": dump(st), "disk": dump(disk), "cameras": cams, "analytics": analytics,
                    "credential_backend": b.creds.backend_name, "config_warning": b.repo.load_warning}

        @app.get("/api/settings")
        async def get_settings(request: Request) -> dict[str, Any]:
            b._session(request, "operator")
            return dump(b.repo.snapshot().settings)

        @app.patch("/api/settings")
        async def patch_settings(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            b._session(request, "admin")

            def mutate(cfg: AppConfig) -> None:
                merged = cfg.settings.model_dump()
                for k, v in body.items():
                    if isinstance(v, dict) and isinstance(merged.get(k), dict):
                        merged[k].update(v)
                    else:
                        merged[k] = v
                cfg.settings = SystemSettings.model_validate(merged)

            await b._update("settings", mutate)
            return dump(b.repo.snapshot().settings)

        @app.get("/api/settings/retention")
        async def get_retention(request: Request) -> dict[str, Any]:
            b._session(request, "operator")
            return dump(b.repo.snapshot().settings.retention)

        @app.put("/api/settings/retention")
        async def put_retention(request: Request, body: RetentionSettings) -> dict[str, Any]:
            b._session(request, "admin")

            def mutate(cfg: AppConfig) -> None:
                cfg.settings.retention = body

            await b._update("settings", mutate)
            return dump(body)

        @app.get("/api/analytics/rules")
        async def rules(request: Request, camera_id: str | None = None) -> list[dict[str, Any]]:
            b._session(request, "operator")
            return [dump(r) for r in b.repo.snapshot().analytics_rules if camera_id is None or r.camera_id == camera_id]

        @app.post("/api/analytics/rules", status_code=201)
        async def create_rule(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            b._session(request, "admin")
            body.pop("id", None)
            try:
                rule = RuleAdapter.validate_python(body)
            except ValidationError as exc:
                raise ValidationFailed("Regla no válida", details={"fields": [
                    {"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}) from exc

            def mutate(cfg: AppConfig) -> None:
                if not cfg.camera(rule.camera_id):
                    raise NotFoundError("Cámara no encontrada")
                cfg.analytics_rules.append(rule)

            await b._update("analytics", mutate)
            return dump(rule)

        @app.get("/api/analytics/rules/{rule_id}")
        async def get_rule(request: Request, rule_id: str) -> dict[str, Any]:
            b._session(request, "operator")
            r = next((r for r in b.repo.snapshot().analytics_rules if r.id == rule_id), None)
            if not r:
                raise NotFoundError("Regla no encontrada")
            return dump(r)

        @app.put("/api/analytics/rules/{rule_id}")
        async def put_rule(request: Request, rule_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
            b._session(request, "admin")
            body["id"] = rule_id
            body["updated_at"] = iso(utcnow())
            try:
                rule = RuleAdapter.validate_python(body)
            except ValidationError as exc:
                raise ValidationFailed("Regla no válida", details={"fields": [
                    {"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}) from exc

            def mutate(cfg: AppConfig) -> None:
                for i, r in enumerate(cfg.analytics_rules):
                    if r.id == rule_id:
                        if r.kind != rule.kind or r.camera_id != rule.camera_id:
                            raise ValidationFailed("No se puede cambiar el tipo ni la cámara de una regla")
                        cfg.analytics_rules[i] = rule
                        return
                raise NotFoundError("Regla no encontrada")

            await b._update("analytics", mutate)
            return dump(rule)

        @app.delete("/api/analytics/rules/{rule_id}", status_code=204)
        async def delete_rule(request: Request, rule_id: str) -> Response:
            b._session(request, "admin")

            def mutate(cfg: AppConfig) -> None:
                before = len(cfg.analytics_rules)
                cfg.analytics_rules = [r for r in cfg.analytics_rules if r.id != rule_id]
                if len(cfg.analytics_rules) == before:
                    raise NotFoundError("Regla no encontrada")

            await b._update("analytics", mutate)
            return Response(status_code=204)

        @app.get("/api/analytics/cameras")
        async def analytics_cameras(request: Request) -> list[dict[str, Any]]:
            b._session(request, "operator")
            return [dump(a) for a in b.repo.snapshot().analytics_cameras]

        @app.put("/api/analytics/cameras/{camera_id}")
        async def put_analytics_camera(request: Request, camera_id: str, body: CameraAnalytics) -> dict[str, Any]:
            b._session(request, "admin")
            if body.camera_id != camera_id:
                raise ValidationFailed("El id de cámara no coincide")

            def mutate(cfg: AppConfig) -> None:
                if not cfg.camera(camera_id):
                    raise NotFoundError("Cámara no encontrada")
                cfg.analytics_cameras = [a for a in cfg.analytics_cameras if a.camera_id != camera_id] + [body]

            await b._update("analytics", mutate)
            return dump(body)

        @app.get("/api/analytics/status")
        async def analytics_status(request: Request) -> dict[str, Any]:
            b._session(request, "operator")
            return b.opt.analytics_status() if b.opt.analytics_status else {"running": False}

        @app.get("/api/events")
        async def events(request: Request) -> StreamingResponse:
            b._session(request)
            q: asyncio.Queue[str] = asyncio.Queue()
            b.listeners.add(q)

            async def gen() -> AsyncIterator[str]:
                try:
                    yield ": ping\n\n"
                    while True:
                        try:
                            msg = await asyncio.wait_for(q.get(), timeout=b.opt.ping_interval)
                        except asyncio.TimeoutError:
                            msg = ": ping\n\n"
                        if await request.is_disconnected():
                            break
                        yield msg
                finally:
                    b.listeners.discard(q)

            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(self.opt.status_interval)
            if not self.listeners:
                continue
            try:
                cfg, paths = self.repo.snapshot(), await self._paths()
                st = await self.engine.status()
                cams = [{"camera_id": c.id, "online": self._live(c, paths)["online"],
                         "recording": self._live(c, paths)["recording"]} for c in cfg.cameras]
                self._broadcast("status", {"cameras": cams, "engine": {"running": st.running}})
            except Exception:  # el bucle de estado nunca debe morir en silencio
                log.exception("Fallo en el bucle de estado del stub")


# ====================================================================== utilidades de demo/pruebas
async def fake_discover(timeout: float) -> list[DiscoveredDevice]:
    await asyncio.sleep(min(timeout, 0.3))
    return [DiscoveredDevice(host="192.168.1.64", http_port=80, vendor_guess="hikvision", model="DS-2CD2143G2-I",
                             name="Entrada"),
            DiscoveredDevice(host="192.168.1.108", http_port=80, vendor_guess="dahua", model="NVR4108HS-8P-4KS2",
                             name="NVR almacén")]


def rtsp_snapshot(engine: Any) -> Callable[[str, str], Awaitable[bytes]]:
    """Captura un frame del RTSP local de MediaMTX con OpenCV, en memoria (nunca a disco)."""

    async def grab(camera_id: str, stream: str) -> bytes:
        def work() -> bytes:
            import cv2  # opencv-python-headless
            cap = cv2.VideoCapture(engine.rtsp_read_url(camera_id, stream), cv2.CAP_FFMPEG)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        if ok:
                            return bytes(buf)
                raise EngineUnavailable("No se pudo capturar una imagen de la cámara")
            finally:
                cap.release()

        return await asyncio.to_thread(work)

    return grab


def main() -> None:  # pragma: no cover - herramienta manual
    from tests.fakes import FakeEngine
    from tests.web.demo import ServerThread
    parser = argparse.ArgumentParser(description="Backend de pruebas de la interfaz web")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--data", type=Path, default=Path(".tmp/web-demo"))
    parser.add_argument("--camsim", action="store_true",
                        help="simulador de cámaras + MediaMTX real, con dos NVR ya dados de alta")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def stop_signal(signum: int, frame: object) -> None:
        raise KeyboardInterrupt  # misma limpieza ordenada que con Ctrl+C (sin procesos huérfanos)

    signal.signal(signal.SIGTERM, stop_signal)
    sim = None
    if args.camsim:
        from tests.web.demo import start_demo
        sim, engine, options = start_demo(args.data.resolve())
    else:
        engine = FakeEngine()
        options = StubOptions(discover=fake_discover)
    backend = StubBackend(args.data / "data", engine, options)
    server = ServerThread(backend.app, port=args.port).start()
    try:
        if sim is not None and not backend.repo.config.devices:
            api = httpx.Client(base_url=server.base_url, headers={"X-Requested-With": "vms"}, timeout=30)
            api.post("/api/auth/login", json={"username": "admin", "password": DEFAULT_USERS["admin"][0]})
            for name, label in (("hik1", "NVR Hikvision (simulado)"), ("dah1", "NVR Dahua (simulado)")):
                dev = sim.device(name)
                api.post("/api/devices", json={"name": label, "vendor": dev.vendor, "kind": "nvr", "host": "127.0.0.1",
                                               "rtsp_port": dev.port, "username": dev.username,
                                               "password": dev.password, "import_channels": "all"})
            ids = [c["id"] for c in api.get("/api/cameras").json()]
            api.put("/api/walls/1", json={"name": "Demostración", "grid": 4, "cells": ids[:4]})
            api.close()
        print(f"\nInterfaz: {server.base_url}/   ·   admin / {DEFAULT_USERS['admin'][0]}"
              f"   ·   muro: {server.base_url}/wall/1\nCtrl+C para salir.\n", flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        stop = getattr(engine, "stop_sync", None)
        if stop:
            stop()
        if sim is not None:
            sim.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
