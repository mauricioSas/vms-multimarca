"""Autenticación HTTP Digest (RFC 7616, MD5, qop=auth) y Basic para los mocks ASGI."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Literal

from starlette.requests import Request
from starlette.responses import Response

_PARAM_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]*))')


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class HttpAuthChecker:
    def __init__(self, username: str, password: str, realm: str,
                 mode: Literal["digest", "basic", "none"] = "digest") -> None:
        self.username, self.password, self.realm, self.mode = username, password, realm, mode
        self._nonces: set[str] = set()
        self.failures = 0

    def challenge(self, body: str = "", media_type: str = "text/plain") -> Response:
        if self.mode == "digest":
            nonce = secrets.token_hex(16)
            self._nonces.add(nonce)
            header = f'Digest qop="auth", realm="{self.realm}", nonce="{nonce}", stale="FALSE"'
        else:
            header = f'Basic realm="{self.realm}"'
        return Response(body, status_code=401, media_type=media_type, headers={"WWW-Authenticate": header})

    def check(self, request: Request) -> bool:
        if self.mode == "none":
            return True
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        ok = False
        if self.mode == "basic" and scheme.lower() == "basic":
            try:
                user, _, pw = base64.b64decode(value).decode("utf-8").partition(":")
                ok = hmac.compare_digest(user, self.username) and hmac.compare_digest(pw, self.password)
            except (ValueError, UnicodeDecodeError):
                ok = False
        elif self.mode == "digest" and scheme.lower() == "digest":
            p = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
                 for m in _PARAM_RE.finditer(value)}
            if p.get("username") == self.username and p.get("nonce") in self._nonces:
                uri = p.get("uri", "")
                ha1 = _md5(f"{self.username}:{self.realm}:{self.password}")
                ha2 = _md5(f"{request.method}:{uri}")
                if p.get("qop"):
                    expected = _md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:{p['qop']}:{ha2}")
                else:
                    expected = _md5(f"{ha1}:{p['nonce']}:{ha2}")
                ok = hmac.compare_digest(expected, p.get("response", ""))
        if not ok:
            self.failures += 1
        return ok
