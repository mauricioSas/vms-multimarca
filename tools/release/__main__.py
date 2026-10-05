"""`python -m tools.release …` — publicar versiones (guía completa: docs/PUBLICAR-VERSION.md).

    keys init-dev [--keys DIR] [--pkcs11-lib LIB --token-label vms-dev --pin PIN]
    keys show
    init                                     crea los repositorios online y offline (1.root.json)
    package --component app --src DIR --out ZIP
    publish --version X.Y.Z --artifacts DIR [--channel pilot] [--dry-run] [--meta local|ci] …
    channel NOMBRE (--version X.Y.Z | --pause | --resume)
    data advisories ARCHIVO.json
    mirror --version X.Y.Z --out CARPETA [--days 60]
    verify [--mode online|offline] [--file]
    sign-meta [--mode online] [--timestamp-only]   (CI: publish-meta.yml y timestamp.yml)
    root-rotate --revoke KEYNAME [--add-soft NOMBRE --role root|targets] (ensayo «Si roban una llave»)
    upload --to CARPETA                            (servidor estático; R2 cuando exista la cuenta, D5)
    site-token add|revoke|revoke-client|list --client C [--site S] [--kv-file F] [--out secrets.json]

Por defecto el llavero está en `~/.vms-dev-keys` (`--keys` o `VMS_RELEASE_KEYS`) y los repositorios en
`~/.vms-release/repo` (`--repo` o `VMS_RELEASE_REPO`). Nada de eso vive en el repositorio de código.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import keys as keymod
from .tuf_repo import MODES, TufRepository


def _print(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _keys_dir(a: argparse.Namespace) -> Path:
    return Path(a.keys or os.environ.get("VMS_RELEASE_KEYS") or keymod.DEFAULT_DIR).expanduser()


def _repo_dir(a: argparse.Namespace) -> Path:
    return Path(a.repo or os.environ.get("VMS_RELEASE_REPO") or Path.home() / ".vms-release" / "repo").expanduser()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tools.release")
    ap.add_argument("--keys", help="carpeta del llavero (fuera del repositorio)")
    ap.add_argument("--repo", help="carpeta de los repositorios TUF")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keys")
    ksub = k.add_subparsers(dest="keys_cmd", required=True)
    ki = ksub.add_parser("init-dev")
    ki.add_argument("--pkcs11-lib", default=os.environ.get("PYKCS11LIB", ""))
    ki.add_argument("--token-label", default="vms-dev")
    ki.add_argument("--pin", default=os.environ.get("VMS_RELEASE_PIN", ""))
    ki.add_argument("--force", action="store_true")
    ksub.add_parser("show")

    sub.add_parser("init")

    pk = sub.add_parser("package")
    pk.add_argument("--component", required=True)
    pk.add_argument("--src", required=True, type=Path)
    pk.add_argument("--out", required=True, type=Path)

    p = sub.add_parser("publish")
    p.add_argument("--version", required=True)
    p.add_argument("--artifacts", required=True, type=Path)
    p.add_argument("--channel")
    p.add_argument("--notes-es", default="")
    p.add_argument("--min-from")
    p.add_argument("--security", action="store_true")
    p.add_argument("--severity", default="medium", choices=["low", "medium", "high", "critical"])
    p.add_argument("--config-schema", type=int)
    p.add_argument("--authenticode-o", help="razón social del certificado (sin certificado todavía: N1)")
    p.add_argument("--authenticode-c", default="ES")
    p.add_argument("--authenticode-issuer", action="append", default=[])
    p.add_argument("--accept-irreversible", action="store_true")
    p.add_argument("--meta", choices=["local", "ci"], default=None,
                   help="quién firma snapshot/timestamp del repositorio online (dev: local)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=Path, help="con --dry-run: deja aquí el repositorio resultante")

    c = sub.add_parser("channel")
    c.add_argument("name")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--version")
    g.add_argument("--pause", action="store_true")
    g.add_argument("--resume", action="store_true")
    c.add_argument("--meta", choices=["local", "ci"], default=None)

    d = sub.add_parser("data")
    d.add_argument("kind", choices=["advisories"])
    d.add_argument("file", type=Path)
    d.add_argument("--meta", choices=["local", "ci"], default=None)

    m = sub.add_parser("mirror")
    m.add_argument("--version", required=True)
    m.add_argument("--out", required=True, type=Path)
    m.add_argument("--days", type=int, default=60)

    v = sub.add_parser("verify")
    v.add_argument("--mode", choices=list(MODES), default=None)
    v.add_argument("--file", action="store_true", help="leer como file:// (espejo)")
    v.add_argument("--dir", type=Path, help="carpeta concreta (p. ej. un espejo USB) en vez del repositorio")

    s = sub.add_parser("sign-meta")
    s.add_argument("--mode", choices=list(MODES), default="online")
    s.add_argument("--timestamp-only", action="store_true")

    r = sub.add_parser("root-rotate")
    r.add_argument("--role", default="root", choices=["root", "targets", "snapshot", "timestamp"])
    r.add_argument("--revoke", action="append", default=[], help="nombre de la clave del llavero que sale")
    r.add_argument("--add-soft", help="nombre de una clave software nueva (solo desarrollo y ensayos)")

    u = sub.add_parser("upload")
    u.add_argument("--to", required=True)
    u.add_argument("--mode", choices=list(MODES), default="online")

    t = sub.add_parser("site-token")
    t.add_argument("action", choices=["add", "revoke", "revoke-client", "list"])
    t.add_argument("--client", required=True)
    t.add_argument("--site")
    t.add_argument("--kv-file", type=Path)
    t.add_argument("--out", type=Path, help="con add: escribe secrets.json para el instalador")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return _run(a)
    except (keymod.KeyringError, Exception) as exc:  # noqa: BLE001 - mensaje claro y código 1
        from .publish import PublishError
        from .site_token import TokenError
        from .tuf_repo import RepoError

        if isinstance(exc, (keymod.KeyringError, PublishError, RepoError, TokenError, FileNotFoundError)):
            sys.stderr.write(f"Error: {exc}\n")
            return 1
        raise


def _run(a: argparse.Namespace) -> int:
    from .publish import (PublishOptions, Repos, dry_run_copy, publish, publish_advisories, set_channel)

    if a.cmd == "keys":
        if a.keys_cmd == "init-dev":
            kr = keymod.init_dev(_keys_dir(a), pkcs11_lib=a.pkcs11_lib, token_label=a.token_label, pin=a.pin,
                                 force=a.force)
            _print(keymod.describe(kr))
            return 0
        _print(keymod.describe(keymod.Keyring.load(_keys_dir(a))))
        return 0
    if a.cmd == "package":
        from .package import make_zip
        _print({"zip": str(a.out), "sha256": make_zip(a.src, a.out, component=a.component)})
        return 0
    if a.cmd == "site-token":
        return _site_token(a)
    if a.cmd == "verify" and a.dir:
        from .verify import verify_dir
        _print(verify_dir(a.dir, mode=a.mode or "offline", use_file=a.file))
        return 0

    kr = keymod.Keyring.load(_keys_dir(a))
    meta_default = "local" if kr.env == "dev" else "ci"
    base = _repo_dir(a)
    if a.cmd == "init":
        repos = Repos.open(base, kr, create=True)
        _print({"repo": str(base), "env": kr.env, "online_root": str(repos.online.meta_dir / "1.root.json"),
                "offline_root": str(repos.offline.meta_dir / "1.root.json")})
        return 0
    if a.cmd == "publish":
        auth = None
        if a.authenticode_o:
            auth = {"subject_o": a.authenticode_o, "subject_c": a.authenticode_c,
                    "issuers": a.authenticode_issuer or ["Certum Extended Validation Code Signing 2021 CA"]}
        opt = PublishOptions(version=a.version, artifacts=a.artifacts, channel=a.channel, notes_es=a.notes_es,
                             min_from=a.min_from, security=a.security, severity=a.severity,
                             config_schema=a.config_schema, authenticode=auth,
                             accept_irreversible=a.accept_irreversible, meta=a.meta or meta_default)
        if a.dry_run:
            from .verify import verify_dir
            work = dry_run_copy(base)
            repos = Repos.open(work, kr, create=True)
            opt.meta = "local"
            summary = publish(repos, opt)
            summary["verify"] = {m: verify_dir(work / m, mode=m, versions=[a.version]) for m in MODES}
            summary["dry_run"] = True
            if a.out:
                shutil.copytree(work, a.out, dirs_exist_ok=True)
                summary["out"] = str(a.out)
            shutil.rmtree(work.parent, ignore_errors=True)
            _print(summary)
            return 0
        _print(publish(Repos.open(base, kr), opt))
        return 0
    if a.cmd == "channel":
        repos = Repos.open(base, kr)
        pause = True if a.pause else (False if a.resume else None)
        _print(set_channel(repos, a.name, version=a.version, pause=pause, meta=a.meta or meta_default))
        return 0
    if a.cmd == "data":
        _print(publish_advisories(Repos.open(base, kr), a.file, meta=a.meta or meta_default))
        return 0
    if a.cmd == "mirror":
        from .mirror import make_mirror
        from .verify import verify_dir
        res = make_mirror(Repos.open(base, kr), a.version, a.out, days=a.days)
        res["verify"] = verify_dir(a.out, mode="offline", use_file=True, versions=[a.version])
        _print(res)
        return 0
    if a.cmd == "verify":
        from .verify import verify_dir
        modes = [a.mode] if a.mode else list(MODES)
        _print({m: verify_dir(base / m, mode=m, use_file=a.file) for m in modes})
        return 0
    if a.cmd == "sign-meta":
        repo = TufRepository(base, a.mode).load()
        repo.write_snapshot_timestamp(kr, timestamp_only=a.timestamp_only)
        _print({"mode": a.mode, "snapshot": repo.snapshot.signed.version, "timestamp": repo.timestamp.signed.version,
                "expires": repo.timestamp.signed.expires.isoformat()})
        return 0
    if a.cmd == "root-rotate":
        return _root_rotate(a, kr, base)
    if a.cmd == "upload":
        return _upload(base / a.mode, a.to)
    return 2


def _root_rotate(a: argparse.Namespace, kr: keymod.Keyring, base: Path) -> int:
    """Sustituye claves de un rol en `root` (con 2 de 3 llaves `root`) y vuelve a firmar lo necesario."""
    from .publish import Repos

    repos = Repos.open(base, kr)
    revoked = [kr.keys[n] for n in a.revoke]
    # El root N+1 lo firma el umbral del root N SIN las claves que salen (pueden estar perdidas o robadas)…
    old_names = [e.name for e in kr.for_role("root") if e.name not in a.revoke]
    if len(old_names) < keymod.ROOT_THRESHOLD:
        raise keymod.KeyringError(f"Hacen falta {keymod.ROOT_THRESHOLD} claves root que sigan siendo válidas")
    role_in_root = {"offline-timestamp": "timestamp"}.get(a.role, a.role)
    added = []
    if a.add_soft:
        added.append(keymod.add_software_key(kr, a.add_soft, [a.role]))
    for n in a.revoke:
        kr.keys.pop(n)
    kr.validate()
    kr.save()
    # …y el umbral del root N+1 (si entra una clave root nueva, firma también).
    new_names = [e.name for e in kr.for_role("root")]
    names = list(dict.fromkeys([*old_names[: keymod.ROOT_THRESHOLD], *new_names[: keymod.ROOT_THRESHOLD]]))
    if a.add_soft and a.role == "root" and a.add_soft not in names:
        names.append(a.add_soft)
    signers = [kr.signer(n) for n in names]
    out = {}
    for r in repos.each():
        v = r.rotate_root(signers=signers, revoke=[e.keyid for e in revoked], add=[e.public for e in added],
                          role=role_in_root)
        out[r.mode] = v
    repos.commit(meta="local")
    _print({"root_versions": out, "revoked": a.revoke, "added": [e.name for e in added]})
    return 0


def _upload(mode_dir: Path, to: str) -> int:
    """Sube a un servidor estático (carpeta). Orden: targets → N.*.json → timestamp.json (el último)."""
    if to.startswith("r2://"):
        sys.stderr.write("Subir a R2 necesita la cuenta de Cloudflare (decisión D5): pendiente.\n")
        return 1
    dest = Path(to)
    for sub in ("targets",):
        src = mode_dir / sub
        for p in sorted(src.rglob("*")):
            if p.is_file():
                d = dest / sub / p.relative_to(src)
                if not d.exists():
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, d)
    meta = mode_dir / "metadata"
    for p in sorted(meta.iterdir()):
        if p.name != "timestamp.json":
            (dest / "metadata").mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / "metadata" / p.name)
    shutil.copy2(meta / "timestamp.json", dest / "metadata" / "timestamp.json")
    _print({"uploaded_to": str(dest)})
    return 0


def _site_token(a: argparse.Namespace) -> int:
    from . import site_token as st

    kv: st.KV = st.LocalKV(a.kv_file) if a.kv_file else st.CloudflareKV.from_env()
    if a.action == "add":
        if not a.site:
            raise st.TokenError("Falta --site")
        token = st.add(kv, a.client, a.site)
        if a.out:
            st.write_secrets(a.out, token)
            _print({"client": a.client, "site": a.site, "secrets": str(a.out)})
        else:
            _print({"client": a.client, "site": a.site, "token": token,
                    "aviso": "Se muestra una sola vez: guárdalo en el secrets.json de la tienda"})
        return 0
    if a.action == "revoke":
        if not a.site:
            raise st.TokenError("Falta --site")
        _print({"revoked": st.revoke(kv, a.client, a.site)})
        return 0
    if a.action == "revoke-client":
        _print({"deleted": st.revoke_client(kv, a.client)})
        return 0
    _print(st.list_sites(kv, a.client))
    return 0


if __name__ == "__main__":
    sys.exit(main())
