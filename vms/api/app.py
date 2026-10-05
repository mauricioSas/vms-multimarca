"""Aplicación FastAPI del backend VMS (CONTRATO §5.3 y §6).

    app = create_app(settings)                   # producción: motor MediaMTX y fabricantes reales
    app = create_app(settings, engine=FakeEngine(), client_factory=...)   # pruebas

Arranque (lifespan): carpetas → sede → usuario admin inicial → proxy HTTP interno →
motor (start + apply) → suscripción a cambios de configuración (antirrebote 1 s) → latido.
Parada: latido → motor → proxy.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vms import APP_NAME, __version__, web
from vms.core.webmime import ensure_web_mimetypes
from vms.core.config_store import ConfigRepository, ConfigStore, UserStore
from vms.core.credentials import CredentialStore
from vms.core.interfaces import Engine
from vms.core.models import AppConfig, User
from vms.core.settings import VmsSettings, load_settings

from . import errors
from .errors import error_response
from .events import publish_engine
from .routes import ROUTERS
from .security import CSRF_HEADER, CSRF_VALUE, KioskSigner, SessionStore, hash_password
from .state import AppState, ClientFactory, DeviceTester, Discoverer

ensure_web_mimetypes()

log = logging.getLogger("vms.api")

ENGINE_RETRY_S = 30.0
MODIFYING = {"POST", "PUT", "PATCH", "DELETE"}


# Defensa en profundidad frente a XSS: solo scripts propios (sin inline ni eval). Los estilos en línea
# se permiten porque la interfaz posiciona elementos con atributos style (línea de tiempo, zonas).
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")


class SecurityMiddleware:
    """CSRF (cabecera X-Requested-With: vms en toda petición /api que modifica) + cabeceras seguras."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"].startswith("/api/") and scope["method"] in MODIFYING:
            value = ""
            for k, v in scope.get("headers") or []:
                if k.decode("latin-1").lower() == CSRF_HEADER:
                    value = v.decode("latin-1")
            if value.strip().lower() != CSRF_VALUE:
                resp = error_response("csrf", "Petición rechazada: falta la cabecera de seguridad "
                                              "«X-Requested-With: vms»", 403)
                await resp(scope, receive, send)
                return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "same-origin")
                if headers.get("content-type", "").startswith("text/html"):
                    headers.setdefault("Content-Security-Policy", CSP)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _default_client_factory() -> ClientFactory:
    from vms.vendors import client_for
    return lambda device, password: client_for(device, password)


def _default_tester() -> DeviceTester:
    from vms.vendors import test_device
    return lambda device, password: test_device(device, password)


def _default_discoverer() -> Discoverer:
    from vms.vendors import discover
    return lambda timeout: discover(timeout)


def _default_engine(settings: VmsSettings) -> Engine:
    from vms.engine import MediaMtxEngine
    return MediaMtxEngine(settings, settings.paths)


def build_state(settings: VmsSettings, *, engine: Engine | None = None,
                credential_store: CredentialStore | None = None, client_factory: ClientFactory | None = None,
                device_tester: DeviceTester | None = None, discoverer: Discoverer | None = None,
                apply_delay: float = 1.0) -> AppState:
    paths = settings.paths.ensure()
    repo = ConfigRepository(ConfigStore(paths.config_file))
    users = UserStore(paths.users_file)
    creds = credential_store or CredentialStore.create(
        settings.credential_backend, paths.secrets_dir,
        settings.secret_key.get_secret_value() if settings.secret_key else None)
    state = AppState(
        settings=settings, paths=paths, repo=repo, users=users, creds=creds,
        engine=engine or _default_engine(settings),
        client_factory=client_factory or _default_client_factory(),
        device_tester=device_tester or _default_tester(),
        discoverer=discoverer or _default_discoverer(),
        internal_token=settings.ensure_internal_token(),
        sessions=SessionStore(settings.session_hours, kiosk_signer=(
            KioskSigner(settings.kiosk_token.get_secret_value())
            if settings.kiosk_token and settings.kiosk_token.get_secret_value() else None)),
        apply_delay=apply_delay,
    )
    state.kiosk_token()   # firma de las cookies de kiosco con el token vigente (secrets/kiosk.token o el .env)
    return state


async def _prepare(state: AppState) -> None:
    s = state.settings
    cfg = state.config()
    if cfg.settings.site.id == "site-local" and s.site_id != "site-local":
        def seed(c: AppConfig) -> None:
            c.settings.site.id = s.site_id
        await state.repo.update(seed)
        log.info("Identificador de sede tomado de VMS_SITE_ID: %s", s.site_id)
    if not state.users.all():
        if s.admin_initial_password and len(s.admin_initial_password.get_secret_value()) >= 8:
            await state.users.save_user(User(username="admin", role="admin",
                                             password_hash=hash_password(s.admin_initial_password.get_secret_value())))
            log.warning("Se creó el usuario administrador «admin» con la contraseña de VMS_ADMIN_INITIAL_PASSWORD. "
                        "Cámbiala desde el panel y borra esa variable del .env.")
        else:
            if s.admin_initial_password:
                log.error("VMS_ADMIN_INITIAL_PASSWORD tiene menos de 8 caracteres; no se usó")
            log.warning("No hay usuarios. Crea el administrador desde este mismo equipo en "
                        "http://127.0.0.1:%s/setup (o define VMS_ADMIN_INITIAL_PASSWORD en el .env).", s.http_port)


async def _engine_keeper(state: AppState) -> None:
    """Si el motor no arrancó (binario ausente, puerto ocupado...), se reintenta cada 30 s."""
    while not state.engine_started:
        await asyncio.sleep(ENGINE_RETRY_S)
        if await state.start_engine():
            log.info("El motor de vídeo arrancó en un reintento")


async def _start_heartbeat(state: AppState) -> Any:
    dsn = state.settings.pg_dsn
    if not dsn:
        return None
    try:
        from central.heartbeat import HeartbeatSender
    except ImportError as exc:
        log.warning("No se enviará el latido a la central: falta el módulo central.heartbeat (%s)", exc)
        return None
    try:
        sender = HeartbeatSender(dsn.get_secret_value(), state.site, state.settings.heartbeat_seconds,
                                 state.heartbeat_payload)
        await sender.start()
        log.info("Latido hacia la central activado cada %d s", state.settings.heartbeat_seconds)
        return sender
    except Exception:  # noqa: BLE001 - el latido nunca debe impedir arrancar
        log.exception("No se pudo arrancar el latido hacia la central")
        return None


def create_app(settings: VmsSettings | None = None, *, engine: Engine | None = None,
               credential_store: CredentialStore | None = None, client_factory: ClientFactory | None = None,
               device_tester: DeviceTester | None = None, discoverer: Discoverer | None = None,
               start_engine: bool = True, heartbeat: bool = True, apply_delay: float = 1.0) -> FastAPI:
    settings = settings or load_settings()
    state = build_state(settings, engine=engine, credential_store=credential_store, client_factory=client_factory,
                        device_tester=device_tester, discoverer=discoverer, apply_delay=apply_delay)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await _prepare(state)
        # El proxy WHEP/reproducción se identifica ante MediaMTX con el usuario interno del backend
        # (MediaMTX no admite peticiones anónimas: vms.core.mtx_auth).
        creds_fn = getattr(state.engine, "http_credentials", None)
        proxy_auth = creds_fn() if callable(creds_fn) else None
        state.proxy = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False,
                                        auth=proxy_auth)
        keeper: asyncio.Task[None] | None = None
        sender: Any = None
        # Evento SSE `engine` (CONTRATO §17.3): el motor avisa al arrancar, reiniciarse o caer y los muros ponen a
        # cero su espera en vez de descubrirlo por su cuenta. Puede llegar desde otro hilo: se publica en el bucle.
        if hasattr(state.engine, "on_event"):
            loop = asyncio.get_running_loop()

            def on_engine_event(ev: Any) -> None:
                loop.call_soon_threadsafe(publish_engine, state.bus, ev)

            state.engine.on_event = on_engine_event
        if start_engine:
            if not await state.start_engine():
                keeper = asyncio.create_task(_engine_keeper(state), name="engine-keeper")
        state.repo.subscribe(lambda _cfg: state.schedule_apply())
        if heartbeat:
            sender = await _start_heartbeat(state)
        log.info("%s %s listo en http://%s:%s", APP_NAME, __version__, settings.http_host, settings.http_port)
        try:
            yield
        finally:
            log.info("Deteniendo %s", APP_NAME)
            if sender is not None:
                try:
                    await sender.stop()
                except Exception:  # noqa: BLE001
                    log.exception("Error al detener el latido")
            if keeper is not None:
                keeper.cancel()
            try:
                await asyncio.wait_for(state.flush_apply(), 10.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                log.warning("Se descartó una aplicación de configuración pendiente al parar")
            try:
                await state.engine.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error al detener el motor de vídeo")
            await state.proxy.aclose()
            state.proxy = None

    app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.vms = state
    errors.install(app)
    for router in ROUTERS:
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR, check_dir=False), name="static")
    app.mount("/vendor", StaticFiles(directory=web.WEB_DIR / "vendor", check_dir=False), name="vendor")
    app.add_middleware(SecurityMiddleware)
    return app
