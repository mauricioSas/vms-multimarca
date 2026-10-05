"""Autenticación HTTP para los mocks ASGI: Digest RFC 7616 (MD5 o SHA-256, qop=auth) y Basic.

Modos: «digest» (MD5, como la mayoría de equipos), «digest-sha256» (Hikvision recientes), «digest-both»
(dos cabeceras WWW-Authenticate: SHA-256 y MD5), «basic» y «none». Cuenta las peticiones que llegan con
credenciales (`credentialed`) para comprobar el «un solo intento» del alta.
"""
from __future__ import annotations

import base64
import hmac
import re
import secrets
from typing import Literal

from starlette.requests import Request
from starlette.responses import Response

from vms.vendors.auth import verify_digest

AuthMode = Literal["digest", "digest-sha256", "digest-both", "basic", "none"]


class HttpAuthChecker:
    def __init__(self, username: str, password: str, realm: str, mode: AuthMode = "digest") -> None:
        self.username, self.password, self.realm, self.mode = username, password, realm, mode
        self._nonces: set[str] = set()
        self.failures = 0              # peticiones rechazadas (también el primer reto sin credenciales)
        self.credentialed = 0          # peticiones que trajeron Authorization
        self.rejected_credentials = 0  # de esas, las rechazadas (contraseña mala)

    def challenge(self, body: str = "", media_type: str = "text/plain", status: int = 401) -> Response:
        resp = Response(body, status_code=status, media_type=media_type)
        if self.mode.startswith("digest"):
            algs = {"digest": ["MD5"], "digest-sha256": ["SHA-256"], "digest-both": ["SHA-256", "MD5"]}[self.mode]
            for alg in algs:
                nonce = secrets.token_hex(16)
                self._nonces.add(nonce)
                resp.headers.append("WWW-Authenticate", f'Digest qop="auth", realm="{self.realm}", nonce="{nonce}", '
                                                        f'algorithm={alg}, stale="FALSE"')
        else:
            resp.headers.append("WWW-Authenticate", f'Basic realm="{self.realm}"')
        return resp

    def check(self, request: Request) -> bool:
        if self.mode == "none":
            return True
        header = request.headers.get("authorization", "")
        if header:
            self.credentialed += 1
        scheme, _, value = header.partition(" ")
        ok = False
        if self.mode == "basic" and scheme.lower() == "basic":
            try:
                user, _, pw = base64.b64decode(value).decode("utf-8").partition(":")
                ok = hmac.compare_digest(user, self.username) and hmac.compare_digest(pw, self.password)
            except (ValueError, UnicodeDecodeError):
                ok = False
        elif self.mode.startswith("digest") and scheme.lower() == "digest":
            ok = verify_digest(header, request.method, self.username, self.password, self.realm, self._nonces)
            if ok and self.mode == "digest-sha256" and not re.search(r'algorithm="?SHA-256', header, re.IGNORECASE):
                ok = False
        if not ok:
            self.failures += 1
            if header:
                self.rejected_credentials += 1
        return ok
