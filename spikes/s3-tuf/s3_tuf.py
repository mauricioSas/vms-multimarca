"""Prueba de concepto S3: firma TUF con ECDSA P-256 (software o PKCS#11) y verificación con python-tuf 7.

    python spikes/s3-tuf/s3_tuf.py --signer software
    python spikes/s3-tuf/s3_tuf.py --signer pkcs11 --pkcs11-lib /usr/lib/softhsm/libsofthsm2.so \
        --token-label vms-dev --pin 1234

Qué comprueba (PLAN-V2 §1.6 y §6.1, S3):
  1. `root` (3 claves ECDSA P-256, umbral 2) y `targets` (ECDSA P-256) firmados con el firmante elegido.
     Con `--signer pkcs11` dos claves de `root` y la de `targets` viven en el token (SoftHSM en CI,
     YubiKey en producción) y se firman con `HSMSigner` de securesystemslib (python-pkcs11, MIT).
     La tercera clave de `root` es software (la «clave en papel»).
  2. `snapshot` y `timestamp` con ed25519 (software, como el secreto del flujo de CI).
  3. Un cliente `ngclient.Updater` descarga un target por HTTP y lo verifica.
  4. Casos negativos: target alterado, `targets` firmado con una clave no autorizada, `timestamp`
     caducado, versión anterior de `snapshot` (rollback) y rotación de `root` 1 → 2 (con 2 de 3).

Todas las claves son de DESARROLLO: se generan en una carpeta temporal y se borran al terminar.
Nunca se guardan en el repositorio. Sale con código 0 solo si todos los casos dan lo esperado y
escribe el resumen en JSON (`--out`).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from securesystemslib.signer import CryptoSigner, HSMSigner, Signer, SSlibKey
from tuf.api.exceptions import (BadVersionNumberError, ExpiredMetadataError, LengthOrHashMismatchError,
                                UnsignedMetadataError)
from tuf.api.metadata import (SPECIFICATION_VERSION, DelegatedRole, Metadata, MetaFile, Root, Snapshot,
                              TargetFile, Targets, Timestamp)
from tuf.api.serialization.json import JSONSerializer
from tuf.ngclient import Updater

TARGET_NAME = "components/app/app-2.1.0.zip"
TARGET_BYTES = b"PK\x03\x04 contenido de prueba de S3 " * 64
_ = DelegatedRole  # (sin delegaciones en la v2; se importa para documentar que no se usan)


def utc_in(days: float) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


# --------------------------------------------------------------------------- firmantes
def make_pkcs11_keys(lib: str, token_label: str, pin: str, key_ids: list[int]) -> dict[int, SSlibKey]:
    """Genera (si no existen) pares ECDSA P-256 en el token y devuelve sus claves públicas."""
    import pkcs11
    import pkcs11.util
    from pkcs11.exceptions import NoSuchKey
    from pkcs11.util.ec import encode_named_curve_parameters

    os.environ["PYKCS11LIB"] = lib
    token = pkcs11.lib(lib).get_token(token_label=token_label)
    out: dict[int, SSlibKey] = {}
    with token.open(rw=True, user_pin=pin) as session:
        for kid in key_ids:
            id_bytes = pkcs11.util.biginteger(kid)
            try:
                session.get_key(pkcs11.ObjectClass.PRIVATE_KEY, pkcs11.KeyType.EC, id=id_bytes)
            except NoSuchKey:
                params = session.create_domain_parameters(
                    pkcs11.KeyType.EC, {pkcs11.Attribute.EC_PARAMS: encode_named_curve_parameters("secp256r1")},
                    local=True)
                params.generate_keypair(store=True, id=id_bytes, label=f"vms-dev-tuf-{kid}")
    for kid in key_ids:
        _uri, pub = HSMSigner.import_(hsm_keyid=kid, token_label=token_label, secrets_handler=lambda _s: pin)
        out[kid] = pub
    return out


class Keyring:
    """Firmantes de cada rol. `root` 2 de 3, `targets` 1, `snapshot` 1, `timestamp` 1."""

    def __init__(self, mode: str, *, lib: str = "", token_label: str = "", pin: str = "") -> None:
        self.mode = mode
        self.signers: dict[str, list[Signer]] = {}
        if mode == "software":
            self.signers["root"] = [CryptoSigner.generate_ecdsa() for _ in range(3)]
            self.signers["targets"] = [CryptoSigner.generate_ecdsa()]
        else:
            pubs = make_pkcs11_keys(lib, token_label, pin, [11, 12, 21])
            uri = f"hsm:{{}}?label={token_label}"

            def hsm(kid: int) -> Signer:
                return Signer.from_priv_key_uri(uri.format(kid), pubs[kid], lambda _s: pin)
            self.signers["root"] = [hsm(11), hsm(12), CryptoSigner.generate_ecdsa()]   # la 3.ª = «papel»
            self.signers["targets"] = [hsm(21)]
        self.signers["snapshot"] = [CryptoSigner.generate_ed25519()]
        self.signers["timestamp"] = [CryptoSigner.generate_ed25519()]

    def key(self, role: str, i: int = 0) -> SSlibKey:
        k = self.signers[role][i].public_key
        assert isinstance(k, SSlibKey)
        return k


# --------------------------------------------------------------------------- repositorio
class Repo:
    """Repositorio TUF mínimo en disco con `consistent_snapshot` (como el de §2.7)."""

    def __init__(self, base: Path, keys: Keyring) -> None:
        self.base = base
        self.meta_dir = base / "metadata"
        self.tgt_dir = base / "targets"
        self.meta_dir.mkdir(parents=True)
        self.tgt_dir.mkdir(parents=True)
        self.keys = keys
        self.ser = JSONSerializer(compact=False)

        root = Root(expires=utc_in(365), consistent_snapshot=True)
        for role in ("root", "targets", "snapshot", "timestamp"):
            for i in range(len(keys.signers[role])):
                root.add_key(keys.key(role, i), role)
        root.roles["root"].threshold = 2
        self.root = Metadata(root)
        self.targets = Metadata(Targets(expires=utc_in(90)))
        self.snapshot = Metadata(Snapshot(expires=utc_in(30)))
        self.timestamp = Metadata(Timestamp(expires=utc_in(7)))

    # -- firma y escritura
    def sign(self, md: Metadata[Any], role: str, signers: list[Signer] | None = None) -> None:
        md.signatures.clear()
        for s in signers if signers is not None else self.keys.signers[role]:
            md.sign(s, append=True)

    def write_root(self, signers: list[Signer] | None = None) -> None:
        self.sign(self.root, "root", signers if signers is not None else self.keys.signers["root"][:2])
        self.root.to_file(str(self.meta_dir / f"{self.root.signed.version}.root.json"), self.ser)

    def add_target(self, name: str, data: bytes) -> None:
        tf = TargetFile.from_data(name, data, ["sha256"])
        self.targets.signed.targets[name] = tf
        h = tf.hashes["sha256"]
        path = self.tgt_dir / Path(name).parent / f"{h}.{Path(name).name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def publish(self, *, bump: bool = True, targets_signers: list[Signer] | None = None,
                timestamp_expires: datetime | None = None) -> None:
        """targets → snapshot → timestamp, cada uno con versión nueva."""
        if bump:
            self.targets.signed.version += 1
        self.sign(self.targets, "targets", targets_signers)
        self.targets.to_file(str(self.meta_dir / f"{self.targets.signed.version}.targets.json"), self.ser)
        self.snapshot.signed.meta["targets.json"] = MetaFile(version=self.targets.signed.version)
        if bump:
            self.snapshot.signed.version += 1
        self.sign(self.snapshot, "snapshot")
        self.snapshot.to_file(str(self.meta_dir / f"{self.snapshot.signed.version}.snapshot.json"), self.ser)
        self.timestamp.signed.snapshot_meta = MetaFile(version=self.snapshot.signed.version)
        if bump:
            self.timestamp.signed.version += 1
        if timestamp_expires is not None:
            self.timestamp.signed.expires = timestamp_expires
        self.sign(self.timestamp, "timestamp")
        self.timestamp.to_file(str(self.meta_dir / "timestamp.json"), self.ser)


@contextlib.contextmanager
def http_server(directory: Path) -> Iterator[str]:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - firma de la clase base
        pass


def new_client(work: Path, url: str, trusted_root: bytes, name: str) -> Updater:
    cdir = work / "clients" / name
    shutil.rmtree(cdir, ignore_errors=True)
    (cdir / "metadata").mkdir(parents=True)
    (cdir / "targets").mkdir(parents=True)
    return Updater(metadata_dir=str(cdir / "metadata"), metadata_base_url=f"{url}/metadata/",
                   target_dir=str(cdir / "targets"), target_base_url=f"{url}/targets/", bootstrap=trusted_root)


# --------------------------------------------------------------------------- casos
def run(mode: str, lib: str, token_label: str, pin: str) -> dict[str, Any]:
    results: dict[str, Any] = {"signer": mode, "tuf_spec": SPECIFICATION_VERSION, "cases": {}}
    t_start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="vms-s3-") as tmp:
        work = Path(tmp)
        keys = Keyring(mode, lib=lib, token_label=token_label, pin=pin)
        results["key_types"] = {r: sorted({keys.key(r, i).keytype + "/" + keys.key(r, i).scheme
                                            for i in range(len(keys.signers[r]))}) for r in keys.signers}
        # Prueba de que las firmas de root/targets salen del token: clase del firmante de cada clave
        results["signer_classes"] = {r: [type(s).__name__ for s in keys.signers[r]] for r in keys.signers}
        repo = Repo(work / "repo", keys)
        repo.write_root()
        trusted_root = (repo.meta_dir / "1.root.json").read_bytes()
        repo.add_target(TARGET_NAME, TARGET_BYTES)
        repo.publish()

        def case(name: str, fn: Callable[[], str]) -> None:
            try:
                results["cases"][name] = {"ok": True, "detail": fn()}
            except Exception as exc:  # noqa: BLE001 - un caso que falla se informa, no aborta el resto
                results["cases"][name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

        def expect(exc_type: type[BaseException], fn: Callable[[], object]) -> str:
            try:
                fn()
            except exc_type as exc:
                return f"rechazado como se esperaba: {type(exc).__name__}"
            raise AssertionError(f"se esperaba {exc_type.__name__} y no se lanzó")

        with http_server(repo.base) as url:
            # 1. descarga correcta
            def ok_download() -> str:
                up = new_client(work, url, trusted_root, "ok")
                up.refresh()
                info = up.get_targetinfo(TARGET_NAME)
                assert info is not None, "el target no aparece en targets.json"
                path = up.download_target(info)
                assert Path(path).read_bytes() == TARGET_BYTES
                return f"descargado y verificado ({info.length} bytes, sha256 {info.hashes['sha256'][:12]}…)"
            case("descarga_valida", ok_download)

            # 2. target alterado en el servidor
            def tampered() -> str:
                h = repo.targets.signed.targets[TARGET_NAME].hashes["sha256"]
                f = repo.tgt_dir / Path(TARGET_NAME).parent / f"{h}.{Path(TARGET_NAME).name}"
                original = f.read_bytes()
                f.write_bytes(original[:-1] + b"X")
                try:
                    up = new_client(work, url, trusted_root, "tampered")
                    up.refresh()
                    info = up.get_targetinfo(TARGET_NAME)
                    assert info is not None
                    return expect(LengthOrHashMismatchError, lambda: up.download_target(info))
                finally:
                    f.write_bytes(original)
            case("target_alterado", tampered)

            # 3. targets firmado con una clave no autorizada (la de un atacante con la cuenta de CI)
            def rogue_targets() -> str:
                saved = copy.deepcopy((repo.targets, repo.snapshot, repo.timestamp))
                repo.publish(targets_signers=[CryptoSigner.generate_ecdsa()])
                try:
                    up = new_client(work, url, trusted_root, "rogue")
                    return expect(UnsignedMetadataError, up.refresh)
                finally:
                    repo.targets, repo.snapshot, repo.timestamp = saved
                    repo.publish()   # vuelve a un estado bueno con versiones nuevas
            case("targets_clave_no_autorizada", rogue_targets)

            # 4. timestamp caducado (metadatos congelados)
            def expired() -> str:
                repo.publish(timestamp_expires=utc_in(-1))
                try:
                    up = new_client(work, url, trusted_root, "expired")
                    return expect(ExpiredMetadataError, up.refresh)
                finally:
                    repo.timestamp.signed.expires = utc_in(7)
                    repo.publish()
            case("timestamp_caducado", expired)

            # 5. rollback: el cliente ya vio la versión N; el servidor vuelve a servir un timestamp anterior
            def rollback() -> str:
                up = new_client(work, url, trusted_root, "rollback")
                up.refresh()
                ts_now = (repo.meta_dir / "timestamp.json").read_bytes()
                old = copy.deepcopy(repo.timestamp)
                old.signed.version -= 1
                repo.sign(old, "timestamp")
                old.to_file(str(repo.meta_dir / "timestamp.json"), repo.ser)
                try:
                    cdir = work / "clients" / "rollback"
                    up2 = Updater(metadata_dir=str(cdir / "metadata"), metadata_base_url=f"{url}/metadata/",
                                  target_dir=str(cdir / "targets"), target_base_url=f"{url}/targets/",
                                  bootstrap=None)
                    return expect(BadVersionNumberError, up2.refresh)
                finally:
                    (repo.meta_dir / "timestamp.json").write_bytes(ts_now)
            case("rollback_timestamp", rollback)

            # 6. rotación de root 1 → 2 (2 de las 3 claves viejas + clave nueva que sustituye a la 3.ª)
            def rotate_root() -> str:
                old_signers = keys.signers["root"]
                new_key = CryptoSigner.generate_ecdsa()
                compromised = old_signers[2].public_key.keyid
                repo.root.signed.revoke_key(compromised, "root")
                repo.root.signed.add_key(new_key.public_key, "root")
                repo.root.signed.version += 1
                # 2.root.json lo firman 2 de las claves de 1.root (encadenamiento) y 2 de las nuevas
                signers = [old_signers[0], old_signers[1], new_key]
                repo.write_root(signers=signers)
                keys.signers["root"] = [old_signers[0], old_signers[1], new_key]
                up = new_client(work, url, trusted_root, "rotated")   # el cliente solo confía en 1.root
                up.refresh()
                assert up._trusted_set.root.version == 2  # noqa: SLF001 - comprobación de la prueba
                assert compromised not in up._trusted_set.root.roles["root"].keyids  # noqa: SLF001
                return "el cliente siguió 1.root → 2.root y ya no acepta la clave sustituida"
            case("rotacion_root", rotate_root)

    results["seconds"] = round(time.perf_counter() - t_start, 2)
    results["all_ok"] = all(c["ok"] for c in results["cases"].values())
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--signer", choices=["software", "pkcs11"], default="software")
    ap.add_argument("--pkcs11-lib", default=os.environ.get("PYKCS11LIB", ""))
    ap.add_argument("--token-label", default="vms-dev")
    ap.add_argument("--pin", default=os.environ.get("VMS_DEV_TOKEN_PIN", ""))
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.signer == "pkcs11" and not (a.pkcs11_lib and a.pin):
        ap.error("--signer pkcs11 necesita --pkcs11-lib y --pin (o PYKCS11LIB y VMS_DEV_TOKEN_PIN)")
    res = run(a.signer, a.pkcs11_lib, a.token_label, a.pin)
    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    return 0 if res["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
