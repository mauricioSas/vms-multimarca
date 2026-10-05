"""Autenticación HTTP/RTSP de los equipos: Digest RFC 7616 (SHA-256 y MD5) y Basic.

Reglas (PLAN-V2 §3.2 puntos 1 y 3):
- Se leen **todas** las cabeceras `WWW-Authenticate` (un equipo puede ofrecer varias) y se elige la más
  fuerte que se soporta: Digest SHA-256 > Digest MD5 > Basic.
- Basic solo si el equipo tiene `allow_basic=true` (envía la contraseña sin cifrar por la red).
- `qop=auth` (con `nc` y `cnonce`), `opaque`, `userhash` y el `-sess` de RFC 7616.
- Una sola ronda: el que llama manda las credenciales una vez; si el equipo responde 401 otra vez,
  no se reintenta (muchos equipos bloquean el usuario tras 3-5 fallos).

Nada de este módulo registra contraseñas ni respuestas Digest.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Literal

Scheme = Literal["digest-sha256", "digest-md5", "basic"]

# Orden de preferencia: el primero que aparece en el reto gana.
STRENGTH: dict[str, int] = {"digest-sha256": 3, "digest-md5": 2, "basic": 1}

_PARAM_RE = re.compile(r'([\w-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]*))')
# Inicio de un reto dentro de una cabecera que trae varios separados por coma («Digest …, Basic …»).
_SCHEME_START_RE = re.compile(r'(?:^|,)\s*(Digest|Basic|Bearer|Negotiate|NTLM)\s+', re.IGNORECASE)


@dataclass(frozen=True)
class Challenge:
    scheme: Scheme | Literal["unsupported"]
    realm: str = ""
    nonce: str = ""
    opaque: str = ""
    qop: tuple[str, ...] = ()
    algorithm: str = ""
    userhash: bool = False
    stale: bool = False
    raw: str = field(default="", repr=False)

    @property
    def strength(self) -> int:
        return STRENGTH.get(self.scheme, 0)


def _params(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _PARAM_RE.finditer(text):
        value = m.group(2) if m.group(2) is not None else m.group(3)
        out[m.group(1).lower()] = value.replace('\\"', '"')
    return out


def _split_challenges(header: str) -> list[str]:
    """Una cabecera puede llevar varios retos («Digest realm=…, Basic realm=…»)."""
    starts = [m.start(1) for m in _SCHEME_START_RE.finditer(header)]
    if not starts:
        return [header.strip()] if header.strip() else []
    parts: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(header)
        parts.append(header[start:end].strip().rstrip(","))
    return parts


def parse_challenge(text: str) -> Challenge:
    scheme_word, _, rest = text.strip().partition(" ")
    sw = scheme_word.lower()
    p = _params(rest)
    if sw == "basic":
        return Challenge("basic", realm=p.get("realm", ""), raw=text)
    if sw != "digest":
        return Challenge("unsupported", raw=text)
    algorithm = p.get("algorithm", "MD5")
    alg = algorithm.upper()
    scheme: Scheme | Literal["unsupported"]
    if alg in ("MD5", "MD5-SESS"):
        scheme = "digest-md5"
    elif alg in ("SHA-256", "SHA-256-SESS"):
        scheme = "digest-sha256"
    else:  # SHA-512-256 y otros: no se usan en cámaras; mejor no adivinar
        scheme = "unsupported"
    qop = tuple(q.strip().lower() for q in p.get("qop", "").split(",") if q.strip())
    return Challenge(scheme, realm=p.get("realm", ""), nonce=p.get("nonce", ""), opaque=p.get("opaque", ""),
                     qop=qop, algorithm=algorithm, userhash=p.get("userhash", "").lower() == "true",
                     stale=p.get("stale", "").lower() == "true", raw=text)


def parse_challenges(headers: list[str]) -> list[Challenge]:
    """Todos los retos de todas las cabeceras `WWW-Authenticate`, en el orden en que llegan."""
    out: list[Challenge] = []
    for h in headers:
        for part in _split_challenges(h):
            out.append(parse_challenge(part))
    return out


def choose(challenges: list[Challenge], *, allow_basic: bool = False) -> Challenge | None:
    """El reto más fuerte que sabemos responder. Basic solo con `allow_basic`."""
    usable = [c for c in challenges if c.scheme != "unsupported" and (c.scheme != "basic" or allow_basic)]
    if not usable:
        return None
    return max(usable, key=lambda c: c.strength)


def only_basic(challenges: list[Challenge]) -> bool:
    known = [c for c in challenges if c.scheme != "unsupported"]
    return bool(known) and all(c.scheme == "basic" for c in known)


def _h(algorithm: str, data: str) -> str:
    if algorithm.upper().startswith("SHA-256"):
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    return hashlib.md5(data.encode("utf-8")).hexdigest()


@dataclass
class DigestState:
    """Estado de un reto aceptado: permite firmar varias peticiones con el mismo nonce (nc creciente)."""

    challenge: Challenge
    nc: int = 0


def authorization(challenge: Challenge, method: str, uri: str, username: str, password: str, *,
                  state: DigestState | None = None, cnonce: str | None = None) -> str:
    """Valor de la cabecera `Authorization` para un reto (Digest o Basic)."""
    if challenge.scheme == "basic":
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    if challenge.scheme == "unsupported":
        raise ValueError("reto de autenticación no soportado")
    alg = challenge.algorithm or "MD5"
    if state is not None:
        state.nc += 1
        nc_value = state.nc
    else:
        nc_value = 1
    cn = cnonce or secrets.token_hex(8)
    ha1 = _h(alg, f"{username}:{challenge.realm}:{password}")
    if alg.upper().endswith("-SESS"):
        ha1 = _h(alg, f"{ha1}:{challenge.nonce}:{cn}")
    ha2 = _h(alg, f"{method}:{uri}")
    user_field = _h(alg, f"{username}:{challenge.realm}") if challenge.userhash else username
    parts = [f'username="{user_field}"', f'realm="{challenge.realm}"', f'nonce="{challenge.nonce}"',
             f'uri="{uri}"']
    if "auth" in challenge.qop:
        ncs = f"{nc_value:08x}"
        response = _h(alg, f"{ha1}:{challenge.nonce}:{ncs}:{cn}:auth:{ha2}")
        parts += [f'response="{response}"', "qop=auth", f"nc={ncs}", f'cnonce="{cn}"']
    else:
        parts.append(f'response="{_h(alg, f"{ha1}:{challenge.nonce}:{ha2}")}"')
    if challenge.opaque:
        parts.append(f'opaque="{challenge.opaque}"')
    parts.append(f"algorithm={alg}")
    if challenge.userhash:
        parts.append("userhash=true")
    return "Digest " + ", ".join(parts)


def verify_digest(header: str, method: str, username: str, password: str, realm: str,
                  nonces: set[str] | None = None) -> bool:
    """Comprueba un `Authorization: Digest …` (lo usan los simuladores y las pruebas)."""
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "digest":
        return False
    p = _params(rest)
    alg = p.get("algorithm", "MD5")
    if nonces is not None and p.get("nonce") not in nonces:
        return False
    expected_user = _h(alg, f"{username}:{realm}") if p.get("userhash", "").lower() == "true" else username
    if p.get("username") != expected_user:
        return False
    uri = p.get("uri", "")
    ha1 = _h(alg, f"{username}:{realm}:{password}")
    if alg.upper().endswith("-SESS"):
        ha1 = _h(alg, f"{ha1}:{p.get('nonce', '')}:{p.get('cnonce', '')}")
    ha2 = _h(alg, f"{method}:{uri}")
    if p.get("qop"):
        expected = _h(alg, f"{ha1}:{p.get('nonce', '')}:{p.get('nc', '')}:{p.get('cnonce', '')}:{p['qop']}:{ha2}")
    else:
        expected = _h(alg, f"{ha1}:{p.get('nonce', '')}:{ha2}")
    return secrets.compare_digest(expected, p.get("response", ""))


__all__ = ["Challenge", "DigestState", "Scheme", "authorization", "choose", "only_basic", "parse_challenge",
           "parse_challenges", "verify_digest"]
