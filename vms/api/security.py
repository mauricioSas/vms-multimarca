"""Sesiones, contraseñas, límite de intentos y comprobación de roles (CONTRATO §6.1 y §6.2).

- Contraseñas de usuario con argon2id (argon2-cffi).
- Sesión = cookie aleatoria «vms_session» (HttpOnly, SameSite=Strict, Secure si HTTPS) guardada
  en memoria del servidor; caduca a las VMS_SESSION_HOURS horas. Reiniciar el backend cierra
  las sesiones de usuario.
- Sesión de kiosco = cookie FIRMADA (HMAC-SHA256 con una clave derivada de VMS_KIOSK_TOKEN) que el
  servidor puede validar sin tenerla en memoria: los muros siguen con vídeo tras un reinicio del
  backend (caída, actualización, reinicio del servicio). Caduca a los KIOSK_MAX_AGE_DAYS días y
  deja de valer en cuanto se cambia VMS_KIOSK_TOKEN.
- Roles: admin > operator > kiosk (kiosco = operador de solo lectura para los muros).
- 5 intentos fallidos en 5 minutos por IP+usuario y 20 por IP (cualquier usuario) → 429 con
  Retry-After. La verificación argon2 va en un hilo, con 2 a la vez como máximo, para que un
  ataque de contraseñas no bloquee la API ni la negociación de vídeo de los muros.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.requests import HTTPConnection

log = logging.getLogger("vms.api.security")

COOKIE_NAME = "vms_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "vms"
INTERNAL_HEADER = "x-vms-internal-token"

SessionRole = Literal["admin", "operator", "kiosk"]
ROLE_RANK: dict[str, int] = {"kiosk": 0, "operator": 1, "admin": 2}

_hasher = PasswordHasher()  # argon2id con los parámetros recomendados por la biblioteca
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def burn_verify(password: str) -> None:
    """Gasta lo mismo que una verificación real (evita distinguir usuarios por el tiempo de respuesta)."""
    verify_password(_DUMMY_HASH, password)


# argon2 tarda decenas de ms de CPU: fuera del bucle asíncrono y como mucho 2 a la vez, para que un
# ataque de contraseñas no retrase la API ni la negociación WebRTC de los muros.
_HASH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2")


async def verify_password_async(password_hash: str | None, password: str) -> bool:
    """Verifica en el grupo de hilos de argon2. Sin hash (usuario inexistente) gasta lo mismo y da False."""
    loop = asyncio.get_running_loop()
    if password_hash is None:
        await loop.run_in_executor(_HASH_POOL, burn_verify, password)
        return False
    return await loop.run_in_executor(_HASH_POOL, verify_password, password_hash, password)


async def hash_password_async(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_HASH_POOL, hash_password, password)


def is_local(conn: HTTPConnection) -> bool:
    host = conn.client.host if conn.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def client_ip(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else "?"


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass
class Session:
    token: str
    username: str
    kiosk: bool
    created: float
    expires: float


KIOSK_USERNAME = "kiosco"
KIOSK_MAX_AGE_DAYS = 30
_KIOSK_PREFIX = "k1"


class KioskSigner:
    """Cookies de kiosco firmadas: «k1.<emitida>.<aleatorio>.<hmac>» (validables sin estado)."""

    def __init__(self, kiosk_token: str, max_age_s: float = KIOSK_MAX_AGE_DAYS * 86400) -> None:
        if not kiosk_token:
            raise ValueError("Falta VMS_KIOSK_TOKEN")
        self._key = hmac.new(kiosk_token.encode("utf-8"), b"vms-kiosk-session-v1", hashlib.sha256).digest()
        self.max_age_s = max_age_s

    def _mac(self, body: str) -> str:
        return hmac.new(self._key, body.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self, now: float | None = None) -> str:
        body = f"{_KIOSK_PREFIX}.{int(now if now is not None else time.time())}.{secrets.token_urlsafe(16)}"
        return f"{body}.{self._mac(body)}"

    def verify(self, token: str, now: float | None = None) -> float | None:
        """Hora de emisión si la cookie es auténtica y no ha caducado; si no, None."""
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != _KIOSK_PREFIX or not parts[1].isdigit():
            return None
        body = ".".join(parts[:3])
        if not constant_eq(parts[3], self._mac(body)):
            return None
        issued = float(parts[1])
        now = time.time() if now is None else now
        if issued > now + 300 or now - issued > self.max_age_s:
            return None
        return issued


@dataclass
class SessionStore:
    hours: int = 12
    kiosk_signer: KioskSigner | None = None
    _sessions: dict[str, Session] = field(default_factory=dict)
    _revoked: set[str] = field(default_factory=set)

    def create(self, username: str, *, kiosk: bool = False) -> Session:
        now = time.time()
        token = self.kiosk_signer.issue(now) if kiosk and self.kiosk_signer else secrets.token_urlsafe(32)
        s = Session(token, username.lower(), kiosk, now, now + self.hours * 3600)
        self._sessions[s.token] = s
        self._purge(now)
        return s

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        s = self._sessions.get(token)
        if s is None:
            return self._restore_kiosk(token)
        if s.expires < time.time():
            self._sessions.pop(token, None)
            return None
        return s

    def _restore_kiosk(self, token: str) -> Session | None:
        """Cookie de kiosco emitida antes de un reinicio del backend: se valida por su firma."""
        if self.kiosk_signer is None or token in self._revoked or not token.startswith(_KIOSK_PREFIX + "."):
            return None
        now = time.time()
        issued = self.kiosk_signer.verify(token, now)
        if issued is None:
            return None
        s = Session(token, KIOSK_USERNAME, True, issued, now + self.hours * 3600)
        self._sessions[token] = s
        self._purge(now)
        log.info("Sesión de kiosco recuperada tras un reinicio del backend")
        return s

    def reset_kiosk(self, signer: KioskSigner | None) -> None:
        """Token de kiosco nuevo (`vmsctl kiosk rotate`): firma nueva y fuera todas las sesiones de kiosco. Las
        cookies firmadas con el token anterior ya no validan: los muros vuelven a entrar con el token nuevo."""
        self.kiosk_signer = signer
        for t in [t for t, s in self._sessions.items() if s.kiosk]:
            self._sessions.pop(t, None)
        self._revoked.clear()

    def touch(self, session: Session) -> None:
        """Caducidad deslizante (sesiones de kiosco): el muro abierto no caduca nunca."""
        session.expires = time.time() + self.hours * 3600

    def delete(self, token: str | None) -> None:
        if token:
            s = self._sessions.pop(token, None)
            if (s is not None and s.kiosk) or token.startswith(_KIOSK_PREFIX + "."):
                self._revoked.add(token)  # una cookie de kiosco cerrada no se puede «recuperar»

    def delete_user(self, username: str, *, keep: str | None = None) -> None:
        """Cierra las sesiones de un usuario (menos la `keep`, p. ej. la de quien hace el cambio)."""
        for t in [t for t, s in self._sessions.items() if s.username == username.lower() and t != keep]:
            self._sessions.pop(t, None)

    def _purge(self, now: float) -> None:
        if len(self._sessions) > 500:
            for t in [t for t, s in self._sessions.items() if s.expires < now]:
                self._sessions.pop(t, None)

    @property
    def max_age(self) -> int:
        return self.hours * 3600


class LoginLimiter:
    """Ventana deslizante de fallos por (IP, usuario). Con usuario "*" sirve de límite por IP."""

    MAX_KEYS = 10_000

    def __init__(self, max_failures: int = 5, window_s: float = 300.0) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._fails: dict[tuple[str, str], list[float]] = {}

    def _recent(self, key: tuple[str, str], now: float) -> list[float]:
        recent = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def retry_after(self, ip: str, username: str) -> int:
        """Segundos que faltan para poder volver a intentarlo (0 = se puede)."""
        now = time.time()
        recent = self._recent((ip, username.lower()), now)
        if len(recent) < self.max_failures:
            return 0
        return max(1, int(self.window_s - (now - recent[0])) + 1)

    def failure(self, ip: str, username: str) -> None:
        key = (ip, username.lower())
        now = time.time()
        self._recent(key, now)
        self._fails.setdefault(key, []).append(now)
        if len(self._fails) > self.MAX_KEYS:
            self._prune(now)

    def _prune(self, now: float) -> None:
        """Acota la memoria ante un barrido de usuarios: caducados fuera y, si aún sobra, los más viejos."""
        for k in [k for k, v in self._fails.items() if not v or now - v[-1] >= self.window_s]:
            self._fails.pop(k, None)
        excess = len(self._fails) - self.MAX_KEYS
        if excess > 0:
            # Primero las claves no bloqueadas y más antiguas: un barrido no desbloquea a nadie.
            order = sorted(self._fails, key=lambda k: (len(self._fails[k]) >= self.max_failures, self._fails[k][-1]))
            for k in order[:excess]:
                self._fails.pop(k, None)

    def success(self, ip: str, username: str) -> None:
        self._fails.pop((ip, username.lower()), None)

    def __len__(self) -> int:
        return len(self._fails)
