"""Ensayo del procedimiento «Si roban una llave» (PLAN-V2 §1.6, docs/PUBLICAR-VERSION.md).

Clave `targets` comprometida: con 2 de las 3 llaves `root` se publica un `root` nuevo que la sustituye, se
vuelve a firmar `targets` con la clave nueva y se publica. El cliente rechaza lo firmado con la clave vieja
(aunque el atacante tenga también las claves de CI de snapshot/timestamp) y acepta lo nuevo.
"""
from __future__ import annotations

import json

from tools.release.__main__ import main as release_main
from tools.release.publish import Repos, channel_doc
from vms_updater.models import bundle_target

from .conftest import Site


def test_compromised_targets_key_is_rotated_and_old_key_rejected(site: Site) -> None:
    f = site.factory
    eng = site.engine()
    assert eng.check().result == "no_update"               # la tienda ya confía en el root 1
    stolen = f.kr.signer("dev-targets")                     # el atacante se lleva la llave de targets

    rc = release_main(["--keys", str(f.kr.path), "--repo", str(f.repo_dir), "root-rotate", "--role", "targets",
                       "--revoke", "dev-targets", "--add-soft", "dev-targets-2"])
    assert rc == 0
    from tools.release.keys import Keyring
    kr2 = Keyring.load(f.kr.path)
    assert "dev-targets" not in kr2.keys and "dev-targets-2" in kr2.keys
    f.kr = kr2
    f.repos = Repos.open(f.repo_dir, kr2)
    assert f.repos.online.root.signed.version == 2

    # el atacante publica una versión maliciosa firmada con la clave robada (+ claves de CI)
    repo = f.repos.online
    saved = {p.name: p.read_bytes() for p in repo.meta_dir.iterdir()}
    evil = b'{"schema": 1}'
    repo.add_target(bundle_target("6.6.6"), evil, {"kind": "bundle", "version": "6.6.6"})
    repo.add_target("channels/stable.json", channel_doc("stable", "6.6.6", paused=False), {"kind": "channel"})
    repo.write_targets([stolen])
    repo.write_snapshot_timestamp(kr2)
    out = site.engine().check()
    assert out.result == "error" and "no son de confianza" in out.message_es
    assert site.pointer().active == "2.0.0"
    # se retira lo del atacante
    for p in repo.meta_dir.iterdir():
        if p.name not in saved:
            p.unlink()
    for name, data in saved.items():
        (repo.meta_dir / name).write_bytes(data)
    f.repos = Repos.open(f.repo_dir, kr2)

    # la publicación legítima con la clave nueva se acepta
    f.release("2.1.0", comps=("app",))
    out = site.engine().check()
    assert out.result == "update_ok", out.message_es
    cached_root = json.loads((site.layout.tuf_metadata_dir / "online" / "root.json").read_text())
    assert cached_root["signed"]["version"] == 2
    assert stolen.public_key.keyid not in cached_root["signed"]["roles"]["targets"]["keyids"]


def test_compromised_root_key_rotated_with_two_of_three(site: Site) -> None:
    f = site.factory
    assert site.engine().check().result == "no_update"
    rc = release_main(["--keys", str(f.kr.path), "--repo", str(f.repo_dir), "root-rotate", "--role", "root",
                       "--revoke", "dev-root-3", "--add-soft", "dev-root-4"])
    assert rc == 0
    from tools.release.keys import Keyring
    f.kr = Keyring.load(f.kr.path)
    f.repos = Repos.open(f.repo_dir, f.kr)
    f.release("2.1.0", comps=("app",))
    assert site.engine().check().result == "update_ok"


def test_rotation_refuses_without_enough_valid_root_keys(site: Site) -> None:
    f = site.factory
    rc = release_main(["--keys", str(f.kr.path), "--repo", str(f.repo_dir), "root-rotate", "--role", "root",
                       "--revoke", "dev-root-1", "--revoke", "dev-root-2"])
    assert rc == 1
