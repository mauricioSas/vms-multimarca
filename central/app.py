"""Panel central multi-sede (FastAPI). Lee la PostgreSQL común y recibe latidos HTTP de las sedes.

API (convenciones de CONTRATO §6.1: JSON, fechas UTC con «Z», errores
`{"error": {"code", "message", "details"}}`, cookie de sesión, CSRF con `X-Requested-With: vms`):

  GET  /api/health                                  —   estado del panel y de la base de datos
  POST /api/auth/login | /api/auth/logout           —/O
  GET  /api/auth/me                                 O
  GET|POST /api/auth/setup                          —   primer administrador (solo desde localhost)
  GET  /api/sites                                   O   sedes con último latido, estado y conteos hoy/semana
  GET  /api/sites/{id}                              O   una sede + cámaras + reglas
  GET  /api/sites/{id}/counts?range=|from=&to=&bucket=hour|day&rule_id=
  GET  /api/sites/{id}/occupancy?range=|from=&to=&bucket=&rule_id=
  GET  /api/sites/{id}/queue-alerts?range=|from=&to=&limit=
  GET  /api/sites/{id}/reports                      O
  GET  /api/sites/{id}/reports/{week_start}         O
  GET  /api/compare?range=|from=&to=                O   comparativa entre sedes
  GET  /api/queues/top?range=|from=&to=&limit=      O   colas con más alertas
  GET  /api/reports/latest                          O   último informe de cada sede
  GET|POST|DELETE /api/site-tokens[/{site_id}]      A   tokens de latido por sede
  GET|POST|PATCH|DELETE /api/users[/{username}]     A
  POST /api/heartbeat                               token de sede (Authorization: Bearer …)

O = operador o administrador; A = administrador.
"""
# Sin «from __future__ import annotations»: FastAPI debe resolver las anotaciones locales
# (Sess, Admin, Conn) al definir las rutas dentro de create_app.
import ipaddress
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg import AsyncConnection
from psycopg import Error as PgError
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import BaseModel, SecretStr, ValidationError

from vms import __version__
from vms.core.config_store import UserStore
from vms.core.errors import (AuthError, ConflictError, ForbiddenError, NotConfigured, NotFoundError,
                             RateLimited, ValidationFailed, VmsError)
from vms.core.models import User, UserCreate, UserPublic, UserUpdate
from vms.core.naming import is_valid_id
from vms.core.rtsp import redact

from . import db
from .extensions import CentralDeps, extension_builders
from .heartbeat import MAX_PAYLOAD_BYTES, HeartbeatIn, record_heartbeat
from .updates import directive_for
from .security import (FailureLimiter, Session, SessionStore, SiteTokenStore, hash_password,
                       hash_password_async, verify_password_async)
from .settings import CentralSettings

from vms.core.webmime import ensure_web_mimetypes

ensure_web_mimetypes()

log = logging.getLogger("central.app")

COOKIE_NAME = "vms_central_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "vms"
WEB_DIR = Path(__file__).resolve().parent / "web"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT = {"/api/heartbeat"}


class DbUnavailable(VmsError):
    code, status = "db_unavailable", 503


# =========================================================================== serialización
def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    return value


def ok(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(_jsonable(data), status_code=status, headers=headers)


def site_state(row: dict[str, Any], now: datetime, default_interval: int, down_after: int) -> dict[str, Any]:
    """Estado de una sede a partir de su último latido."""
    last_seen: datetime | None = row.get("last_seen")
    payload = row.get("payload") or {}
    interval = payload.get("interval_s") if isinstance(payload.get("interval_s"), int) else default_interval
    interval = max(10, int(interval))
    if last_seen is None:
        return {"online": False, "state": "unknown", "age_s": None, "interval_s": interval}
    age = (now - last_seen).total_seconds()
    online = age <= interval * down_after
    state = (row.get("reported_status") or "ok") if online else "down"
    return {"online": online, "state": state, "age_s": round(max(age, 0.0), 1), "interval_s": interval}


def _site_out(row: dict[str, Any], now: datetime, settings: CentralSettings) -> dict[str, Any]:
    payload = row.get("payload") or {}
    health = site_state(row, now, settings.heartbeat_seconds, settings.down_after_intervals)
    disk = payload.get("disk") if isinstance(payload.get("disk"), dict) else {}
    analytics = payload.get("analytics") if isinstance(payload.get("analytics"), dict) else {}
    return {
        "site_id": row["site_id"], "name": row["name"], "code": row["code"], "timezone": row["timezone"],
        "active": row.get("active", True),
        **health,
        "last_seen": row.get("last_seen"), "hostname": row.get("hostname") or "",
        "version": row.get("version") or "", "reported_status": row.get("reported_status"),
        "cameras_total": payload.get("cameras_total"), "cameras_online": payload.get("cameras_online"),
        "disk_percent": disk.get("percent"), "disk_free_gb": disk.get("free_gb"),
        "temperature_c": payload.get("temperature_c"),
        "analytics_running": analytics.get("running"), "analytics_stale": analytics.get("stale"),
        "today": {"in": row["today_in"], "out": row["today_out"], "alerts": row["alerts_today"]},
        "week": {"in": row["week_in"], "out": row["week_out"], "alerts": row["alerts_week"],
                 "prev_in": row["prev_week_in"]},
        "alerts_open": row["alerts_open"],
        "day_start": row["day_start"], "week_start": row["week_start"],
        "last_report_week": row.get("last_report_week"),
    }


# =========================================================================== peticiones
class LoginIn(BaseModel):
    username: str
    password: SecretStr


class SetupIn(BaseModel):
    username: str
    password: SecretStr


def _client_ip(request: Request, trusted: list[str]) -> str:
    """IP del cliente. Detrás de proxies de confianza, X-Forwarded-For se lee de DERECHA a izquierda
    saltando los proxies: el primer valor lo escribe el propio cliente y no es de fiar."""
    host = request.client.host if request.client else ""
    if host not in trusted:
        return host
    hops = [h.strip() for h in ",".join(request.headers.getlist("x-forwarded-for")).split(",") if h.strip()]
    for hop in reversed(hops):
        if hop not in trusted:
            return hop
    return hops[0] if hops else host


def _is_local(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return ip in ("localhost", "testclient")


def _parse_dt(value: str | None, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed(f"Fecha no válida en «{name}»", details={"fields": [
            {"loc": ["query", name], "msg": "Usa ISO 8601, p. ej. 2026-10-04T00:00:00Z"}]}) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# =========================================================================== aplicación
def create_app(settings: CentralSettings, *, pool: AsyncConnectionPool[Any] | None = None,
               clock: Callable[[], datetime] | None = None) -> FastAPI:
    """Crea la aplicación. `pool` y `clock` se pueden inyectar (pruebas)."""
    settings.ensure_dirs()
    users = UserStore(settings.users_file)
    sessions = SessionStore(settings.session_hours)
    tokens = SiteTokenStore(settings.tokens_file)
    login_limiter = FailureLimiter(5, 300)        # por IP + usuario
    ip_limiter = FailureLimiter(20, 300)          # por IP, cualquier usuario
    user_limiter = FailureLimiter(30, 900)        # por usuario, desde cualquier IP (ataque distribuido)
    token_limiter = FailureLimiter(20, 300)
    now_fn: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
    owns_pool = pool is None
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal pool
        if settings.admin_initial_password and not users.all():
            pw = settings.admin_initial_password.get_secret_value()
            if len(pw) >= 8:
                await users.save_user(User(username="admin", role="admin", password_hash=hash_password(pw)))
                log.info("Creado el usuario «admin» del panel central (VMS_CENTRAL_ADMIN_INITIAL_PASSWORD)")
            else:
                log.error("VMS_CENTRAL_ADMIN_INITIAL_PASSWORD es demasiado corta (mínimo 8 caracteres)")
        if pool is None and settings.pg_dsn:
            pool = db.make_pool(settings.pg_dsn.get_secret_value(), settings.pg_pool_max)
            await pool.open(wait=False)
        elif pool is None:
            log.warning("Sin VMS_CENTRAL_PG_DSN / VMS_PG_DSN: el panel arranca sin base de datos")
        log.info("Panel central listo en %s:%s", settings.http_host, settings.http_port)
        try:
            yield
        finally:
            if owns_pool and pool is not None:
                await pool.close()

    app = FastAPI(title="VMS Multimarca · Panel central", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.users = users
    app.state.sessions = sessions
    app.state.tokens = tokens

    # ------------------------------------------------------------------ errores
    @app.exception_handler(VmsError)
    async def _vms_error(_: Request, exc: VmsError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited) and "retry_after" in exc.details:
            headers["Retry-After"] = str(exc.details["retry_after"])
        return JSONResponse(exc.to_dict(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))} for e in exc.errors()]
        err = ValidationFailed("Hay datos no válidos en la petición", details={"fields": fields})
        return JSONResponse(err.to_dict(), status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("Error inesperado: %s", redact(repr(exc)), exc_info=exc)
        err = VmsError("Error interno del panel central. Revisa los registros.", code="internal_error")
        return JSONResponse(err.to_dict(), status_code=500)

    # ------------------------------------------------------------------ CSRF y cabeceras
    @app.middleware("http")
    async def _csrf_and_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        path = request.url.path
        if (request.method in UNSAFE_METHODS and path.startswith("/api/") and path not in CSRF_EXEMPT
                and request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE):
            err = ForbiddenError("Falta la cabecera de protección CSRF (X-Requested-With: vms)", code="csrf")
            return JSONResponse(err.to_dict(), status_code=403)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    # ------------------------------------------------------------------ dependencias
    def current_session(request: Request) -> Session:
        s = sessions.get(request.cookies.get(COOKIE_NAME))
        if s is None:
            raise AuthError("Inicia sesión para continuar")
        user = users.get(s.username)
        if user is None or not user.enabled:
            sessions.delete(s.token)
            raise AuthError("Tu usuario ya no está activo")
        return s

    def require_admin(s: Annotated[Session, Depends(current_session)]) -> Session:
        if s.role != "admin":
            raise ForbiddenError("Solo un administrador puede hacer esto")
        return s

    async def conn() -> AsyncIterator[AsyncConnection[Any]]:
        if pool is None:
            raise NotConfigured("El panel no tiene base de datos configurada (VMS_CENTRAL_PG_DSN)",
                                code="not_configured")
        try:
            async with pool.connection(timeout=5) as c:
                yield c
        except PoolTimeout as exc:
            log.warning("PostgreSQL no responde: %s", redact(str(exc)))
            raise DbUnavailable("La base de datos central no responde. Inténtalo de nuevo en unos minutos.") \
                from exc
        except PgError as exc:
            log.error("Error de PostgreSQL: %s", redact(str(exc)))
            raise DbUnavailable("Error al consultar la base de datos central.") from exc

    Sess = Annotated[Session, Depends(current_session)]
    Admin = Annotated[Session, Depends(require_admin)]
    Conn = Annotated[AsyncConnection[Any], Depends(conn)]

    async def _site_or_404(c: AsyncConnection[Any], site_id: str) -> dict[str, Any]:
        if not is_valid_id(site_id):
            raise NotFoundError("Sede no encontrada")
        row = await db.get_site_row(c, site_id)
        if row is None:
            raise NotFoundError("Sede no encontrada")
        return row

    async def _range(c: AsyncConnection[Any], tz: str, range_name: str | None, from_: str | None,
                     to: str | None, default: str) -> tuple[datetime, datetime, str]:
        start, end = _parse_dt(from_, "from"), _parse_dt(to, "to")
        if start and end:
            if end <= start:
                raise ValidationFailed("El final del rango debe ser posterior al inicio")
            return start, end, "custom"
        name = range_name or default
        if name not in db.RANGE_NAMES:
            raise ValidationFailed(f"Rango desconocido: {name}", details={"fields": [
                {"loc": ["query", "range"], "msg": "Valores: " + ", ".join(db.RANGE_NAMES)}]})
        s, e = await db.resolve_range(c, name, tz, now_fn())
        return start or s, end or e, name

    # ------------------------------------------------------------------ salud
    @app.get("/api/health")
    async def health() -> JSONResponse:
        db_state = "not_configured"
        if pool is not None:
            try:
                async with pool.connection(timeout=3) as c:
                    await c.execute("SELECT 1")
                db_state = "ok"
            except Exception as exc:
                log.warning("Comprobación de salud: base de datos no disponible (%s)", redact(str(exc)))
                db_state = "error"
        status = "ok" if db_state == "ok" else "degraded"
        return ok({"status": status, "version": __version__, "uptime_s": round(time.monotonic() - started, 1),
                   "db": db_state})

    # ------------------------------------------------------------------ autenticación
    def _set_cookie(request: Request, response: Response, token: str) -> None:
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict",
                            secure=settings.secure_cookies or request.url.scheme == "https",
                            max_age=settings.session_hours * 3600, path="/")

    @app.post("/api/auth/login")
    async def login(body: LoginIn, request: Request) -> JSONResponse:
        ip = _client_ip(request, settings.trusted_proxies)
        key = f"{ip}|{body.username.lower()}"
        ip_key = f"ip|{ip}"
        user_key = f"user|{body.username.lower()}"
        wait = max(login_limiter.retry_after(key), ip_limiter.retry_after(ip_key), user_limiter.retry_after(user_key))
        if wait:
            raise RateLimited("Demasiados intentos. Espera unos minutos.", details={"retry_after": wait})
        user = users.get(body.username)
        valid = await verify_password_async(user.password_hash if user else None, body.password.get_secret_value())
        if not user or not valid or not user.enabled:
            login_limiter.fail(key)
            ip_limiter.fail(ip_key)
            user_limiter.fail(user_key)
            log.warning("Inicio de sesión fallido para «%s» desde %s", body.username[:40], ip)
            raise AuthError("Usuario o contraseña incorrectos", code="invalid_credentials")
        login_limiter.reset(key)
        user.last_login_at = datetime.now(timezone.utc)
        await users.save_user(user)
        s = sessions.create(user.username, user.role)
        resp = ok({"user": user.public()})
        _set_cookie(request, resp, s.token)
        log.info("Sesión iniciada: %s (%s)", user.username, user.role)
        return resp

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        sessions.delete(request.cookies.get(COOKIE_NAME))
        resp = Response(status_code=204)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/auth/me")
    async def me(s: Sess) -> JSONResponse:
        return ok({"username": s.username, "role": s.role})

    @app.get("/api/auth/setup")
    async def setup_needed() -> JSONResponse:
        return ok({"needed": not users.all()})

    @app.post("/api/auth/setup", status_code=201)
    async def setup(body: SetupIn, request: Request) -> JSONResponse:
        if users.all():
            raise ConflictError("El panel ya tiene usuarios")
        if not _is_local(_client_ip(request, [])):
            raise ForbiddenError("El primer administrador solo se puede crear desde el propio servidor")
        try:
            data = UserCreate(username=body.username, password=body.password, role="admin")
        except ValidationError as exc:
            raise ValidationFailed("Datos no válidos", details={"fields": [
                {"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}) from exc
        user = User(username=data.username, role="admin",
                    password_hash=await hash_password_async(data.password.get_secret_value()))
        await users.save_user(user)
        log.info("Primer administrador creado: %s", user.username)
        return ok({"user": user.public()}, status=201)

    # ------------------------------------------------------------------ usuarios
    def _enabled_admins(exclude: str | None = None) -> int:
        return sum(1 for u in users.all() if u.role == "admin" and u.enabled
                   and (exclude is None or u.username.lower() != exclude.lower()))

    @app.get("/api/users")
    async def list_users(_: Admin) -> JSONResponse:
        return ok([u.public() for u in users.all()])

    @app.post("/api/users", status_code=201)
    async def create_user(body: UserCreate, _: Admin) -> JSONResponse:
        if users.get(body.username):
            raise ConflictError("Ya existe un usuario con ese nombre")
        user = User(username=body.username, role=body.role,
                    password_hash=await hash_password_async(body.password.get_secret_value()))
        await users.save_user(user)
        log.info("Usuario creado: %s (%s)", user.username, user.role)
        return ok(user.public(), status=201)

    @app.patch("/api/users/{username}")
    async def update_user(username: str, body: UserUpdate, s: Admin) -> JSONResponse:
        user = users.get(username)
        if user is None:
            raise NotFoundError("Usuario no encontrado")
        new_role = body.role or user.role
        new_enabled = user.enabled if body.enabled is None else body.enabled
        if (user.role == "admin" and user.enabled) and (new_role != "admin" or not new_enabled) \
                and _enabled_admins(exclude=user.username) == 0:
            raise ConflictError("Debe quedar al menos un administrador habilitado")
        user.role, user.enabled = new_role, new_enabled
        if body.password is not None:
            user.password_hash = await hash_password_async(body.password.get_secret_value())
        await users.save_user(user)
        if not user.enabled or body.password is not None:
            if user.username.lower() != s.username.lower() or not user.enabled:
                sessions.delete_user(user.username)
        else:
            sessions.update_role(user.username, user.role)
        return ok(user.public())

    @app.delete("/api/users/{username}", status_code=204)
    async def delete_user(username: str, s: Admin) -> Response:
        user = users.get(username)
        if user is None:
            raise NotFoundError("Usuario no encontrado")
        if user.username.lower() == s.username.lower():
            raise ConflictError("No puedes borrar tu propio usuario")
        if user.role == "admin" and user.enabled and _enabled_admins(exclude=user.username) == 0:
            raise ConflictError("Debe quedar al menos un administrador habilitado")
        await users.delete_user(user.username)
        sessions.delete_user(user.username)
        return Response(status_code=204)

    # ------------------------------------------------------------------ tokens de sede
    @app.get("/api/site-tokens")
    async def list_tokens(_: Admin) -> JSONResponse:
        return ok(tokens.list())

    @app.post("/api/site-tokens/{site_id}", status_code=201)
    async def issue_token(site_id: str, s: Admin) -> JSONResponse:
        if not is_valid_id(site_id):
            raise ValidationFailed("Identificador de sede no válido (minúsculas, números y guiones; 3-40)")
        token = tokens.issue(site_id)
        log.info("Token de latido emitido para la sede %s por %s", site_id, s.username)
        return ok({"site_id": site_id, "token": token,
                   "note": "Copia el token ahora: no se vuelve a mostrar. Ponlo en VMS_SITE_TOKEN de la sede."},
                  status=201)

    @app.delete("/api/site-tokens/{site_id}", status_code=204)
    async def revoke_token(site_id: str, s: Admin) -> Response:
        if not tokens.revoke(site_id):
            raise NotFoundError("Esa sede no tiene token")
        log.info("Token de latido revocado para la sede %s por %s", site_id, s.username)
        return Response(status_code=204)

    # ------------------------------------------------------------------ latido HTTP
    @app.post("/api/heartbeat")
    async def heartbeat(request: Request) -> Response:
        ip = _client_ip(request, settings.trusted_proxies)
        wait = token_limiter.retry_after(ip)
        if wait:
            raise RateLimited("Demasiados intentos con un token no válido", details={"retry_after": wait})
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        site_id = tokens.verify(token)
        if site_id is None:
            token_limiter.fail(ip)
            log.warning("Latido rechazado desde %s: token ausente o no válido", ip)
            raise AuthError("Token de sede no válido", code="invalid_token")
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_PAYLOAD_BYTES:
            raise ValidationFailed("El latido es demasiado grande", code="payload_too_large", status=413)
        raw = await request.body()
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValidationFailed("El latido es demasiado grande", code="payload_too_large", status=413)
        try:
            body = HeartbeatIn.model_validate(json.loads(raw or b"null"))
        except (ValueError, ValidationError) as exc:
            fields = ([{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
                      if isinstance(exc, ValidationError) else [{"loc": ["body"], "msg": "JSON no válido"}])
            raise ValidationFailed("Latido con formato no válido", details={"fields": fields}) from exc
        if body.site.id != site_id:
            log.warning("Latido rechazado: el token es de %s pero el cuerpo dice %s", site_id, body.site.id)
            raise ForbiddenError("El token no corresponde a esta sede", code="site_mismatch")
        if pool is None:
            raise NotConfigured("El panel no tiene base de datos configurada")
        try:
            async with pool.connection(timeout=5) as c:
                await record_heartbeat(c, body.site, body.payload)
                # Lo que pide el panel a esta sede viaja en la respuesta (CONTRATO §15.6): el agente lo entrega
                # al actualizador. `payload.update` (lo que informa la sede) se copia en `site_versions`.
                reported = (body.payload.model_extra or {}).get("update")
                directive = await directive_for(c, site_id, now_fn(), reported if isinstance(reported, dict)
                                                else None)
        except PoolTimeout as exc:
            raise DbUnavailable("La base de datos central no responde") from exc
        except PgError as exc:
            log.error("No se pudo guardar el latido de %s: %s", site_id, redact(str(exc)))
            raise DbUnavailable("No se pudo guardar el latido") from exc
        log.debug("Latido de %s guardado (%s)", site_id, body.payload.status)
        return JSONResponse({"directive": jsonable_encoder(directive)})

    # ------------------------------------------------------------------ sedes
    @app.get("/api/sites")
    async def sites(_: Sess, c: Conn) -> JSONResponse:
        now = now_fn()
        rows = await db.list_sites(c, now)
        return ok({"generated_at": now, "down_after_intervals": settings.down_after_intervals,
                   "sites": [_site_out(r, now, settings) for r in rows]})

    @app.get("/api/sites/{site_id}")
    async def site_detail(site_id: str, _: Sess, c: Conn) -> JSONResponse:
        await _site_or_404(c, site_id)
        now = now_fn()
        rows = await db.list_sites(c, now, site_id)
        if not rows:
            raise NotFoundError("Sede no encontrada")
        out = _site_out(rows[0], now, settings)
        payload = rows[0].get("payload") or {}
        out["cameras"] = await db.site_cameras(c, site_id)
        live = {cam.get("camera_id"): cam for cam in payload.get("cameras", []) if isinstance(cam, dict)}
        for cam in out["cameras"]:
            st = live.get(cam["camera_id"], {})
            cam["online"] = st.get("online") if out["online"] else None
            cam["recording"] = st.get("recording") if out["online"] else None
        out["rules"] = await db.site_rules(c, site_id)
        out["engine"] = payload.get("engine") if isinstance(payload.get("engine"), dict) else {}
        out["uptime_s"] = payload.get("uptime_s")
        return ok(out)

    BucketQ = Annotated[Literal["hour", "day"] | None, Query()]

    @app.get("/api/sites/{site_id}/counts")
    async def counts(site_id: str, _: Sess, c: Conn, range: str | None = None,
                     from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None,
                     bucket: BucketQ = None, rule_id: str | None = None) -> JSONResponse:
        site = await _site_or_404(c, site_id)
        start, end, name = await _range(c, site["timezone"], range, from_, to, "today")
        b = bucket or ("hour" if name in ("today", "yesterday") or end - start <= timedelta(days=2) else "day")
        try:
            series = await db.site_counts(c, site_id, site["timezone"], start, end, b, rule_id)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return ok({"site_id": site_id, "timezone": site["timezone"], "range": name, "from": start, "to": end,
                   "bucket": b, "rule_id": rule_id,
                   "total": {"in": sum(r["count_in"] for r in series), "out": sum(r["count_out"] for r in series)},
                   "series": [{"start": r["bucket_start"], "in": r["count_in"], "out": r["count_out"]}
                              for r in series]})

    @app.get("/api/sites/{site_id}/occupancy")
    async def occupancy(site_id: str, _: Sess, c: Conn, range: str | None = None,
                        from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None,
                        bucket: BucketQ = None, rule_id: str | None = None) -> JSONResponse:
        site = await _site_or_404(c, site_id)
        start, end, name = await _range(c, site["timezone"], range, from_, to, "today")
        b = bucket or ("hour" if name in ("today", "yesterday") or end - start <= timedelta(days=2) else "day")
        try:
            series = await db.site_occupancy(c, site_id, site["timezone"], start, end, b, rule_id)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return ok({"site_id": site_id, "timezone": site["timezone"], "range": name, "from": start, "to": end,
                   "bucket": b, "rule_id": rule_id,
                   "series": [{"start": r["bucket_start"], "avg_people": r["avg_people"],
                               "max_people": r["max_people"], "seconds_over_threshold": r["seconds_over_threshold"],
                               "minutes": r["minutes"]} for r in series]})

    @app.get("/api/sites/{site_id}/queue-alerts")
    async def queue_alerts(site_id: str, _: Sess, c: Conn, range: str | None = None,
                           from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None,
                           limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> JSONResponse:
        site = await _site_or_404(c, site_id)
        start, end, name = await _range(c, site["timezone"], range, from_, to, "week")
        rows = await db.site_queue_alerts(c, site_id, start, end, now_fn(), limit)
        return ok({"site_id": site_id, "timezone": site["timezone"], "range": name, "from": start, "to": end,
                   "alerts": rows})

    @app.get("/api/sites/{site_id}/reports")
    async def reports(site_id: str, _: Sess, c: Conn) -> JSONResponse:
        await _site_or_404(c, site_id)
        return ok(await db.list_reports(c, site_id))

    @app.get("/api/sites/{site_id}/reports/{week_start}")
    async def report(site_id: str, week_start: date, _: Sess, c: Conn) -> JSONResponse:
        await _site_or_404(c, site_id)
        row = await db.get_report(c, site_id, week_start)
        if row is None:
            raise NotFoundError("No hay informe de esa semana")
        return ok(row)

    # ------------------------------------------------------------------ multi-sede
    @app.get("/api/compare")
    async def compare(_: Sess, c: Conn, range: str | None = None,
                      from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None,
                      tz: str = "Europe/Madrid") -> JSONResponse:
        if not await db.timezone_exists(c, tz):
            raise ValidationFailed("Zona horaria desconocida")
        start, end, name = await _range(c, tz, range, from_, to, "last7")
        rows = await db.compare_sites(c, start, end, now_fn())
        return ok({"range": name, "from": start, "to": end, "timezone": tz, "sites": rows})

    @app.get("/api/queues/top")
    async def queues_top(_: Sess, c: Conn, range: str | None = None,
                         from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None,
                         limit: Annotated[int, Query(ge=1, le=100)] = 10,
                         tz: str = "Europe/Madrid") -> JSONResponse:
        if not await db.timezone_exists(c, tz):
            raise ValidationFailed("Zona horaria desconocida")
        start, end, name = await _range(c, tz, range, from_, to, "last7")
        rows = await db.top_queues(c, start, end, now_fn(), limit)
        return ok({"range": name, "from": start, "to": end, "timezone": tz, "queues": rows})

    @app.get("/api/reports/latest")
    async def reports_latest(_: Sess, c: Conn) -> JSONResponse:
        return ok(await db.latest_reports(c))

    # ------------------------------------------------------------------ páginas
    def _page(name: str) -> FileResponse:
        return FileResponse(WEB_DIR / name, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    def _logged(request: Request) -> bool:
        s = sessions.get(request.cookies.get(COOKIE_NAME))
        return s is not None

    @app.get("/", include_in_schema=False, response_model=None)
    async def page_index(request: Request) -> Response:
        if not users.all():
            return RedirectResponse("/setup", status_code=303)
        return _page("index.html") if _logged(request) else RedirectResponse("/login", status_code=303)

    @app.get("/login", include_in_schema=False)
    async def page_login() -> FileResponse:
        return _page("login.html")

    @app.get("/setup", include_in_schema=False)
    async def page_setup() -> FileResponse:
        return _page("setup.html")

    @app.get("/sites/{site_id}", include_in_schema=False, response_model=None)
    async def page_site(site_id: str, request: Request) -> Response:
        return _page("site.html") if _logged(request) else RedirectResponse("/login", status_code=303)

    @app.get("/sites/{site_id}/reports/{week_start}", include_in_schema=False, response_model=None)
    async def page_report(site_id: str, week_start: str, request: Request) -> Response:
        return _page("report.html") if _logged(request) else RedirectResponse("/login", status_code=303)

    @app.get("/admin", include_in_schema=False, response_model=None)
    async def page_admin(request: Request) -> Response:
        return _page("admin.html") if _logged(request) else RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------------ extensiones v2 (fase 0)
    # B4 (central/updates.py) y B6 (central/ops.py) registran aquí sus rutas sin tocar este archivo.
    deps = CentralDeps(session=current_session, admin=require_admin, conn=conn, now=now_fn, settings=settings)
    for build in extension_builders():
        app.include_router(build(deps))

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app


# Exportado para las pruebas
__all__ = ["create_app", "site_state", "COOKIE_NAME", "UserPublic"]
