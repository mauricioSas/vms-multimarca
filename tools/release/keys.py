"""Llavero de publicación: qué claves firman cada rol TUF y dónde viven (PLAN-V2 §1.6, CONTRATO §15.4).

`<carpeta>/keyring.json` (FUERA del repositorio: por defecto `~/.vms-dev-keys/`):

    {"schema": 1, "env": "dev",
     "keys": {"dev-root-1": {"roles": ["root"], "uri": "hsm:11?label=vms-dev", "keyid": "…", "public": {…}},
              "dev-root-3": {"roles": ["root"], "uri": "file2:dev-root-3.pem", …},
              "dev-targets": {"roles": ["targets"], "uri": "hsm:21?label=vms-dev", …},
              "dev-snapshot": {"roles": ["snapshot"], "uri": "file2:dev-snapshot.pem", …},
              "dev-timestamp": {"roles": ["timestamp"], "uri": "file2:dev-timestamp.pem", …},
              "dev-offline-timestamp": {"roles": ["offline-timestamp"], …}}}

URIs admitidas:
- `file2:<ruta>` — PEM PKCS#8 (relativa a la carpeta del llavero). Solo desarrollo y la clave `snapshot`/
  `timestamp` del PC de publicación.
- `hsm:<id>?label=<token>` — PKCS#11 (`HSMSigner`, python-pkcs11, MIT): SoftHSM en CI, **YubiKey** en
  producción (módulo `ykcs11`). La biblioteca se indica con `PYKCS11LIB`; el PIN se pide por consola o con
  `VMS_RELEASE_PIN`.
- `envpem:<VARIABLE>` — PEM en una variable de entorno (secreto de GitHub para `snapshot`/`timestamp` en
  `publish-meta.yml` y `timestamp.yml`). Nunca para `root` ni `targets`.

Tipos: `root` y `targets` ECDSA P-256 (lo único que admite `HSMSigner` con YubiKey); `snapshot`,
`timestamp` y `offline-timestamp` ed25519.
"""
from __future__ import annotations

import getpass
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from securesystemslib.signer import CryptoSigner, Signer, SSlibKey

ROLES = ("root", "targets", "snapshot", "timestamp", "offline-timestamp")
ECDSA_ROLES = ("root", "targets")
ROOT_THRESHOLD = 2
DEFAULT_DIR = Path.home() / ".vms-dev-keys"


class KeyringError(Exception):
    pass


@dataclass
class KeyEntry:
    name: str
    roles: list[str]
    uri: str
    public: SSlibKey

    @property
    def keyid(self) -> str:
        return self.public.keyid


@dataclass
class Keyring:
    path: Path
    env: str
    keys: dict[str, KeyEntry] = field(default_factory=dict)
    pin_provider: Callable[[str], str] | None = None
    _cache: dict[str, Signer] = field(default_factory=dict)

    # ------------------------------------------------------------------ carga y guardado
    @classmethod
    def load(cls, folder: Path) -> "Keyring":
        f = Path(folder) / "keyring.json"
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyringError(f"No hay llavero en {folder}: crea uno con «python -m tools.release keys init-dev»") from exc
        except ValueError as exc:
            raise KeyringError(f"{f} no es JSON válido") from exc
        kr = cls(Path(folder), str(raw.get("env", "dev")))
        for name, e in (raw.get("keys") or {}).items():
            pub = SSlibKey.from_dict(e["keyid"], dict(e["public"]))
            kr.keys[name] = KeyEntry(name, list(e["roles"]), e["uri"], pub)
        kr.validate()
        return kr

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        data = {"schema": 1, "env": self.env,
                "keys": {n: {"roles": e.roles, "uri": e.uri, "keyid": e.keyid, "public": e.public.to_dict()}
                         for n, e in sorted(self.keys.items())}}
        f = self.path / "keyring.json"
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def validate(self) -> None:
        for role in ROLES:
            if not self.for_role(role):
                raise KeyringError(f"El llavero no tiene clave para «{role}»")
        if len(self.for_role("root")) < ROOT_THRESHOLD:
            raise KeyringError(f"«root» necesita al menos {ROOT_THRESHOLD} claves")
        for role in ECDSA_ROLES:
            for e in self.for_role(role):
                if e.public.scheme != "ecdsa-sha2-nistp256":
                    raise KeyringError(f"La clave {e.name} de «{role}» tiene que ser ECDSA P-256")
        for e in self.keys.values():
            if any(r in ("root", "targets") for r in e.roles) and e.uri.startswith("envpem:"):
                raise KeyringError(f"{e.name}: root/targets nunca desde una variable de entorno")
            if self.env == "dev" and not e.name.startswith("dev-"):
                raise KeyringError(f"{e.name}: las claves de desarrollo se llaman dev-*")

    def for_role(self, role: str) -> list[KeyEntry]:
        return [e for e in self.keys.values() if role in e.roles]

    # ------------------------------------------------------------------ firmantes
    def _pin(self, label: str) -> str:
        env = os.environ.get("VMS_RELEASE_PIN")
        if env:
            return env
        if self.pin_provider is not None:
            return self.pin_provider(label)
        return getpass.getpass(f"PIN de {label}: ")

    def signer(self, name: str) -> Signer:
        if name in self._cache:
            return self._cache[name]
        e = self.keys[name]
        uri = e.uri
        if uri.startswith("file2:"):
            p = Path(uri[len("file2:"):])
            if not p.is_absolute():
                p = self.path / p
            s: Signer = Signer.from_priv_key_uri(f"file2:{p}", e.public)
        elif uri.startswith("envpem:"):
            var = uri[len("envpem:"):]
            pem = os.environ.get(var, "")
            if not pem:
                raise KeyringError(f"La variable {var} con la clave {name} está vacía")
            s = CryptoSigner(load_pem_private_key(pem.encode(), None), e.public)
        elif uri.startswith("hsm:"):
            if not os.environ.get("PYKCS11LIB"):
                raise KeyringError("Para firmar con la llave física define PYKCS11LIB (módulo PKCS#11)")
            s = Signer.from_priv_key_uri(uri, e.public, lambda sec: self._pin(f"{name} ({sec})"))
        else:
            raise KeyringError(f"URI de clave no admitida para {name}: {uri.split(':', 1)[0]}")
        self._cache[name] = s
        return s

    def signers(self, role: str, names: list[str] | None = None) -> list[Signer]:
        entries = self.for_role(role)
        if names:
            entries = [e for e in entries if e.name in names]
        return [self.signer(e.name) for e in entries]


# --------------------------------------------------------------------------- generación de desarrollo
def write_private_file(path: Path, data: bytes) -> None:
    """Escribe un secreto que **nace** solo legible por el propietario: temporal creado con `O_EXCL` y modo
    0600 (nunca existe un instante con el umask por defecto) y sustitución atómica. En Windows el modo no
    cambia el ACL: la protección la da la carpeta del perfil (`%USERPROFILE%`, solo el usuario y SYSTEM)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _write_private(path: Path, pem: bytes) -> None:
    write_private_file(path, pem)


def init_dev(folder: Path, *, pkcs11_lib: str = "", token_label: str = "vms-dev", pin: str = "",
             force: bool = False) -> Keyring:
    """Claves de DESARROLLO (decisión N2). Con `pkcs11_lib`, dos de `root` y la de `targets` se generan
    dentro del token (SoftHSM); la tercera de `root` es software (la «de papel»)."""
    folder = Path(folder)
    if (folder / "keyring.json").exists() and not force:
        raise KeyringError(f"Ya hay un llavero en {folder} (usa --force para sustituirlo)")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, stat.S_IRWXU)
    except OSError:
        pass
    kr = Keyring(folder, "dev")

    def soft(name: str, roles: list[str], ecdsa: bool) -> None:
        signer = CryptoSigner.generate_ecdsa() if ecdsa else CryptoSigner.generate_ed25519()
        _write_private(folder / f"{name}.pem", signer.private_bytes)
        pub = signer.public_key
        assert isinstance(pub, SSlibKey)
        kr.keys[name] = KeyEntry(name, roles, f"file2:{name}.pem", pub)

    if pkcs11_lib:
        pubs = _pkcs11_keys(pkcs11_lib, token_label, pin, {11: "dev-root-1", 12: "dev-root-2", 21: "dev-targets"})
        kr.keys["dev-root-1"] = KeyEntry("dev-root-1", ["root"], f"hsm:11?label={token_label}", pubs[11])
        kr.keys["dev-root-2"] = KeyEntry("dev-root-2", ["root"], f"hsm:12?label={token_label}", pubs[12])
        kr.keys["dev-targets"] = KeyEntry("dev-targets", ["targets"], f"hsm:21?label={token_label}", pubs[21])
    else:
        soft("dev-root-1", ["root"], True)
        soft("dev-root-2", ["root"], True)
        soft("dev-targets", ["targets"], True)
    soft("dev-root-3", ["root"], True)
    soft("dev-snapshot", ["snapshot"], False)
    soft("dev-timestamp", ["timestamp"], False)
    soft("dev-offline-timestamp", ["offline-timestamp"], False)
    kr.validate()
    kr.save()
    return kr


def _pkcs11_keys(lib: str, token_label: str, pin: str, ids: dict[int, str]) -> dict[int, SSlibKey]:
    import pkcs11
    import pkcs11.util
    from pkcs11.exceptions import NoSuchKey
    from pkcs11.util.ec import encode_named_curve_parameters
    from securesystemslib.signer import HSMSigner

    os.environ["PYKCS11LIB"] = lib
    token = pkcs11.lib(lib).get_token(token_label=token_label)
    with token.open(rw=True, user_pin=pin) as session:
        for kid, label in ids.items():
            id_bytes = pkcs11.util.biginteger(kid)
            try:
                session.get_key(pkcs11.ObjectClass.PRIVATE_KEY, pkcs11.KeyType.EC, id=id_bytes)
            except NoSuchKey:
                params = session.create_domain_parameters(
                    pkcs11.KeyType.EC, {pkcs11.Attribute.EC_PARAMS: encode_named_curve_parameters("secp256r1")},
                    local=True)
                params.generate_keypair(store=True, id=id_bytes, label=label)
    out: dict[int, SSlibKey] = {}
    for kid in ids:
        _uri, pub = HSMSigner.import_(hsm_keyid=kid, token_label=token_label, secrets_handler=lambda _s: pin)
        out[kid] = pub
    return out


def add_software_key(kr: Keyring, name: str, roles: list[str], *, ecdsa: bool = True) -> KeyEntry:
    """Clave software nueva (ensayo de compromiso y rotaciones de desarrollo)."""
    signer = CryptoSigner.generate_ecdsa() if ecdsa else CryptoSigner.generate_ed25519()
    _write_private(kr.path / f"{name}.pem", signer.private_bytes)
    pub = signer.public_key
    assert isinstance(pub, SSlibKey)
    entry = KeyEntry(name, roles, f"file2:{name}.pem", pub)
    kr.keys[name] = entry
    return entry


def describe(kr: Keyring) -> dict[str, Any]:
    return {"env": kr.env, "keys": {n: {"roles": e.roles, "type": e.public.scheme, "keyid": e.keyid[:16],
                                         "where": e.uri.split(":", 1)[0]} for n, e in sorted(kr.keys.items())}}
