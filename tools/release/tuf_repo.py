"""Repositorio TUF en disco con `consistent_snapshot` (PLAN-V2 §2.7), igual en `online` y `offline`.

    <repo>/<modo>/metadata/1.root.json 2.root.json … N.targets.json M.snapshot.json timestamp.json
    <repo>/<modo>/targets/<carpeta>/<sha256>.<archivo>

- `online`: `snapshot`/`timestamp` con sus claves (en producción, el secreto de `publish-meta.yml`);
  caducan a los 30 y 7 días.
- `offline` (espejos USB): mismo `root` de llaves, mismos `targets`, y `snapshot`/`timestamp` firmados con
  `offline-timestamp`, que caducan a los 60 días.
- `root` 2 de 3 (ECDSA P-256, 1 año) y `targets` (ECDSA P-256, 90 días). El `root` de desarrollo lleva
  `"x-vms-env": "dev"` en `signed`: un equipo con el `root` de producción nunca acepta nada firmado así.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from securesystemslib.signer import Signer
from tuf.api.metadata import Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp
from tuf.api.serialization.json import JSONSerializer

from .keys import ROOT_THRESHOLD, Keyring

MODES = ("online", "offline")
EXPIRY_DAYS = {"root": 365, "targets": 90, "snapshot": 30, "timestamp": 7, "offline": 60}
_ROOT_FILE = re.compile(r"^(\d+)\.root\.json$")


class RepoError(Exception):
    pass


def utc_in(days: float, now: datetime | None = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(days=days)).replace(microsecond=0)


class TufRepository:
    def __init__(self, base: Path, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self.base = Path(base) / mode
        self.mode = mode
        self.meta_dir = self.base / "metadata"
        self.tgt_dir = self.base / "targets"
        self.ser = JSONSerializer(compact=False)
        self.root: Metadata[Root]
        self.targets: Metadata[Targets]
        self.snapshot: Metadata[Snapshot]
        self.timestamp: Metadata[Timestamp]

    # ------------------------------------------------------------------ crear y cargar
    @property
    def exists(self) -> bool:
        return (self.meta_dir / "1.root.json").is_file()

    def meta_roles(self) -> tuple[str, str]:
        """Roles del llavero que firman snapshot y timestamp en este modo."""
        if self.mode == "offline":
            return "offline-timestamp", "offline-timestamp"
        return "snapshot", "timestamp"

    def init(self, kr: Keyring, *, now: datetime | None = None) -> None:
        if self.exists:
            raise RepoError(f"Ya existe un repositorio en {self.base}")
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.tgt_dir.mkdir(parents=True, exist_ok=True)
        root = Root(expires=utc_in(EXPIRY_DAYS["root"], now), consistent_snapshot=True)
        if kr.env == "dev":
            root.unrecognized_fields["x-vms-env"] = "dev"
        snap_role, ts_role = self.meta_roles()
        for e in kr.for_role("root"):
            root.add_key(e.public, "root")
        for e in kr.for_role("targets"):
            root.add_key(e.public, "targets")
        for e in kr.for_role(snap_role):
            root.add_key(e.public, "snapshot")
        for e in kr.for_role(ts_role):
            root.add_key(e.public, "timestamp")
        root.roles["root"].threshold = ROOT_THRESHOLD
        self.root = Metadata(root)
        self._sign(self.root, kr.signers("root"))
        self.root.to_file(str(self.meta_dir / "1.root.json"), self.ser)
        self.targets = Metadata(Targets(expires=utc_in(EXPIRY_DAYS["targets"], now)))
        self.snapshot = Metadata(Snapshot(expires=utc_in(self.snapshot_days, now)))
        self.timestamp = Metadata(Timestamp(expires=utc_in(self.timestamp_days, now)))
        self.write_targets(kr.signers("targets"), bump=False, now=now)
        self.write_snapshot_timestamp(kr, bump=False, now=now)

    @property
    def snapshot_days(self) -> int:
        return EXPIRY_DAYS["offline"] if self.mode == "offline" else EXPIRY_DAYS["snapshot"]

    @property
    def timestamp_days(self) -> int:
        return EXPIRY_DAYS["offline"] if self.mode == "offline" else EXPIRY_DAYS["timestamp"]

    def load(self) -> "TufRepository":
        if not self.exists:
            raise RepoError(f"No hay repositorio en {self.base}")
        versions = sorted(int(m.group(1)) for p in self.meta_dir.iterdir() if (m := _ROOT_FILE.match(p.name)))
        self.root = Metadata.from_file(str(self.meta_dir / f"{versions[-1]}.root.json"))
        self.timestamp = Metadata.from_file(str(self.meta_dir / "timestamp.json"))
        snap_v = self.timestamp.signed.snapshot_meta.version
        self.snapshot = Metadata.from_file(str(self.meta_dir / f"{snap_v}.snapshot.json"))
        tgt_v = self.snapshot.signed.meta["targets.json"].version
        self.targets = Metadata.from_file(str(self.meta_dir / f"{tgt_v}.targets.json"))
        return self

    @property
    def is_dev(self) -> bool:
        return self.root.signed.unrecognized_fields.get("x-vms-env") == "dev"

    # ------------------------------------------------------------------ firma
    @staticmethod
    def _sign(md: Metadata[Any], signers: list[Signer]) -> None:
        md.signatures.clear()
        for s in signers:
            md.sign(s, append=True)

    def write_targets(self, signers: list[Signer], *, bump: bool = True, now: datetime | None = None) -> None:
        if bump:
            self.targets.signed.version += 1
        self.targets.signed.expires = utc_in(EXPIRY_DAYS["targets"], now)
        self._sign(self.targets, signers)
        self.targets.to_file(str(self.meta_dir / f"{self.targets.signed.version}.targets.json"), self.ser)

    def write_snapshot_timestamp(self, kr: Keyring, *, bump: bool = True, now: datetime | None = None,
                                 days: float | None = None, timestamp_only: bool = False) -> None:
        snap_role, ts_role = self.meta_roles()
        if not timestamp_only:
            self.snapshot.signed.meta["targets.json"] = MetaFile(version=self.targets.signed.version)
            if bump:
                self.snapshot.signed.version += 1
            self.snapshot.signed.expires = utc_in(days if days is not None else self.snapshot_days, now)
            self._sign(self.snapshot, kr.signers(snap_role))
            self.snapshot.to_file(str(self.meta_dir / f"{self.snapshot.signed.version}.snapshot.json"), self.ser)
        self.timestamp.signed.snapshot_meta = MetaFile(version=self.snapshot.signed.version)
        if bump or timestamp_only:
            self.timestamp.signed.version += 1
        self.timestamp.signed.expires = utc_in(days if days is not None else self.timestamp_days, now)
        self._sign(self.timestamp, kr.signers(ts_role))
        self.timestamp.to_file(str(self.meta_dir / "timestamp.json"), self.ser)

    def rotate_root(self, *, signers: list[Signer], revoke: list[str] = (), add: list[Any] = (),
                    role: str = "root", now: datetime | None = None) -> int:
        """Nuevo `N+1.root.json` (lo firman el umbral del `root` anterior y el del nuevo)."""
        r = self.root.signed
        for keyid in revoke:
            r.revoke_key(keyid, role)
        for key in add:
            r.add_key(key, role)
        r.version += 1
        r.expires = utc_in(EXPIRY_DAYS["root"], now)
        self._sign(self.root, signers)
        self.root.to_file(str(self.meta_dir / f"{r.version}.root.json"), self.ser)
        return r.version

    # ------------------------------------------------------------------ targets
    def target_file_path(self, name: str, sha256: str) -> Path:
        parent, _, base = name.rpartition("/")
        return self.tgt_dir / parent / f"{sha256}.{base}"

    def add_target(self, name: str, source: Path | bytes, custom: dict[str, Any]) -> TargetFile:
        data_path: Path | None = None
        if isinstance(source, bytes):
            tf = TargetFile.from_data(name, source, ["sha256"])
        else:
            data_path = Path(source)
            tf = TargetFile.from_file(name, str(data_path), ["sha256"])
        tf.unrecognized_fields["custom"] = custom
        dest = self.target_file_path(name, tf.hashes["sha256"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            if data_path is not None:
                shutil.copyfile(data_path, dest)
            else:
                assert isinstance(source, bytes)
                dest.write_bytes(source)
        self.targets.signed.targets[name] = tf
        return tf

    def get_target(self, name: str) -> TargetFile | None:
        return self.targets.signed.targets.get(name)

    def read_target(self, name: str) -> bytes:
        tf = self.get_target(name)
        if tf is None:
            raise RepoError(f"«{name}» no está en targets.json")
        data = self.target_file_path(name, tf.hashes["sha256"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != tf.hashes["sha256"]:
            raise RepoError(f"«{name}» en disco no coincide con su hash")
        return data

    def remove_target(self, name: str) -> None:
        self.targets.signed.targets.pop(name, None)

    def all_targets(self) -> dict[str, TargetFile]:
        return dict(self.targets.signed.targets)

    def trusted_root_bytes(self) -> bytes:
        return (self.meta_dir / "1.root.json").read_bytes()


def custom_of(tf: TargetFile) -> dict[str, Any]:
    c = tf.unrecognized_fields.get("custom")
    return c if isinstance(c, dict) else {}
