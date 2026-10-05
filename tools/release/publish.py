"""Publicar una versión: descriptor + componentes en los repositorios `online` y `offline` (PLAN-V2 §1.6, §2.7).

    python -m tools.release publish --version 2.1.0 --artifacts dist/ [--channel pilot] [--dry-run]

1. Toma los zips de componente de `--artifacts` (`components/<c>/<c>-<versión>.zip`, como los deja
   `tools.build`) y comprueba cada uno con el mismo código que usa el actualizador (manifiesto, rutas).
2. Los componentes que no vienen se heredan de la versión publicada inmediatamente anterior (por número de
   versión; mismo hash: no se descargan). Se puede publicar un arreglo menor que la última (p. ej. la 2.1.1
   para `stable` con la 2.2.0 ya en `pilot`) si no existe todavía.
3. Escribe el descriptor `bundles/vms-<X>.json` y lo añade con los componentes a `targets` de los dos
   repositorios, firma `targets` (llave física) y, en desarrollo o con `--meta local`, `snapshot`/`timestamp`.
   En producción, `snapshot`/`timestamp` del repositorio `online` los firma CI (`publish-meta.yml`); los del
   `offline` siempre se firman aquí con `offline-timestamp`.
4. Con `--dry-run` trabaja sobre una copia temporal y la valida con `ngclient` por HTTP: no cambia nada.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vms_updater.models import COMPONENT_ROOTS, COMPONENTS, ReleaseDescriptor, bundle_target, channel_target
from vms_updater.stage import StageError, extract_component, sha256_file
from vms_updater.versioning import Version

from .keys import Keyring
from .tuf_repo import MODES, RepoError, TufRepository, custom_of

PRODUCT = "vms-multimarca"
ALL_APP_SERVICES = ["VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral"]
DEFAULT_RESTART: dict[str, list[str]] = {
    "runtime": ALL_APP_SERVICES, "app": ALL_APP_SERVICES, "engine": ["VMSEngine"], "viewer": [],
    "models": ["VMSAnalytics"], "updater": ["VMSUpdater"],
}
_ZIP = re.compile(r"^(?P<comp>[a-z]+)-(?P<ver>[0-9A-Za-z][0-9A-Za-z._-]*)\.zip$")
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "vms" / "db" / "migrations"
# Windows 10 22H2 (19045): admitido con ESU (decisión D11; PLAN-V2 §2.7). Más alto solo si la versión lo exige.
DEFAULT_WINDOWS_BUILD_MIN = 19045


class PublishError(Exception):
    pass


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Repos:
    base: Path
    kr: Keyring
    online: TufRepository
    offline: TufRepository

    @classmethod
    def open(cls, base: Path, kr: Keyring, *, create: bool = False) -> "Repos":
        repos = []
        for mode in MODES:
            r = TufRepository(base, mode)
            if r.exists:
                r.load()
            elif create:
                r.init(kr)
            else:
                raise RepoError(f"No hay repositorio {mode} en {base}: créalo con «python -m tools.release init»")
            if r.is_dev != (kr.env == "dev"):
                raise RepoError(f"El repositorio {mode} es de {'desarrollo' if r.is_dev else 'producción'} y el "
                                f"llavero de {kr.env}: no se mezclan")
            repos.append(r)
        return cls(Path(base), kr, repos[0], repos[1])

    def each(self) -> Iterator[TufRepository]:
        yield self.online
        yield self.offline

    def add_target(self, name: str, source: Path | bytes, custom: dict[str, Any]) -> None:
        for r in self.each():
            r.add_target(name, source, custom)

    def commit(self, *, meta: str = "local") -> None:
        """Firma `targets` en los dos y `snapshot`/`timestamp` (online solo con meta=local)."""
        signers = self.kr.signers("targets")
        for r in self.each():
            r.write_targets(signers)
        self.offline.write_snapshot_timestamp(self.kr)
        if meta == "local":
            self.online.write_snapshot_timestamp(self.kr)


# --------------------------------------------------------------------------- artefactos y descriptor
def find_component_zips(artifacts: Path) -> dict[str, tuple[Path, str]]:
    out: dict[str, tuple[Path, str]] = {}
    for p in sorted(Path(artifacts).rglob("*.zip")):
        m = _ZIP.match(p.name)
        if not m or m.group("comp") not in COMPONENTS:
            continue
        comp = m.group("comp")
        if comp in out:
            raise PublishError(f"Hay dos zips de «{comp}» en {artifacts}")
        out[comp] = (p, m.group("ver"))
    return out


def check_zip(comp: str, path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="vms-check-") as tmp:
        try:
            extract_component(path, Path(tmp) / "x", roots=COMPONENT_ROOTS.get(comp))
        except StageError as exc:
            raise PublishError(f"{path.name}: {exc.message_es}") from exc


def latest_bundle(repo: TufRepository, *, below: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """La versión publicada más alta (o la más alta por debajo de `below`) y su descriptor."""
    limit = Version.parse(below) if below else None
    best: tuple[Version, str] | None = None
    for name, tf in repo.all_targets().items():
        c = custom_of(tf)
        if c.get("kind") == "bundle" and isinstance(c.get("version"), str):
            v = Version.parse(c["version"])
            if limit is not None and not v < limit:
                continue
            if best is None or v > best[0]:
                best = (v, name)
    if best is None:
        return None
    return str(best[0]), json.loads(repo.read_target(best[1]))


def migrations_after(previous: str | None) -> tuple[str | None, list[str]]:
    """(última migración de la central, migraciones no reversibles posteriores a `previous`)."""
    if not MIGRATIONS_DIR.is_dir():
        return None, []
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        return None, []
    latest = files[-1].name[:4]
    irreversible = []
    for f in files:
        mid = f.name[:4]
        if previous is not None and mid <= previous:
            continue
        head = f.read_text(encoding="utf-8").splitlines()[:5]
        if any(re.match(r"^--\s*reversible:\s*no\b", h.strip(), re.I) for h in head):
            irreversible.append(mid)
    return latest, irreversible


def default_config_schema() -> int:
    try:
        from vms.core.models import CONFIG_VERSION
        return int(CONFIG_VERSION)
    except Exception:  # noqa: BLE001
        return 2


def build_descriptor(version: str, components: dict[str, dict[str, Any]], *, min_from: str | None, security: bool,
                     severity: str, notes_es: str, config_schema: int, db: dict[str, Any],
                     authenticode: dict[str, Any] | None, requires: dict[str, Any] | None = None) -> bytes:
    doc: dict[str, Any] = {
        "schema": 1, "product": PRODUCT, "version": version, "min_from": min_from, "security": security,
        "severity": severity, "notes_es": notes_es, "config_schema": config_schema, "db": db,
        "requires": requires or {"windows_build_min": DEFAULT_WINDOWS_BUILD_MIN, "webview2_min": None},
        "authenticode": authenticode, "components": components,
    }
    ReleaseDescriptor.model_validate(doc)          # el mismo modelo que usa el actualizador
    return (json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass
class PublishOptions:
    version: str
    artifacts: Path
    channel: str | None = None
    notes_es: str = ""
    min_from: str | None = None
    security: bool = False
    severity: str = "medium"
    config_schema: int | None = None
    authenticode: dict[str, Any] | None = None
    accept_irreversible: bool = False
    meta: str = "local"
    windows_build_min: int | None = DEFAULT_WINDOWS_BUILD_MIN   # None = sin mínimo
    webview2_min: str | None = None


def publish(repos: Repos, opt: PublishOptions) -> dict[str, Any]:
    Version.parse(opt.version)
    if repos.online.get_target(bundle_target(opt.version)) is not None:
        raise PublishError(f"La {opt.version} ya está publicada: publica una versión nueva")
    if opt.windows_build_min is not None and opt.windows_build_min < 10240:
        raise PublishError(f"--windows-build-min {opt.windows_build_min} no es un build de Windows 10/11 válido")
    # Herencia y migraciones: desde la publicada inmediatamente anterior a ESTA versión (un arreglo 2.1.1
    # hereda de la 2.1.0 aunque ya exista la 2.2.0).
    prev = latest_bundle(repos.online, below=opt.version)
    newest = latest_bundle(repos.online)
    if opt.channel:
        cur = repos.online.get_target(channel_target(opt.channel))
        if cur is not None:
            cur_version = json.loads(repos.online.read_target(channel_target(opt.channel))).get("version")
            if isinstance(cur_version, str) and Version.parse(cur_version) > Version.parse(opt.version):
                raise PublishError(f"El canal «{opt.channel}» ya está en la {cur_version}, mayor que la "
                                   f"{opt.version}: las tiendas no bajan de versión. Publica el arreglo como "
                                   f"{cur_version.rsplit('.', 1)[0]}.x o en otro canal")
    found = find_component_zips(opt.artifacts)
    comps: dict[str, dict[str, Any]] = {}
    new_files: dict[str, Path] = {}
    for comp, (path, cver) in found.items():
        check_zip(comp, path)
        target = f"components/{comp}/{path.name}"
        ref = {"version": cver, "target": target, "sha256": sha256_file(path), "length": path.stat().st_size,
               "restart": DEFAULT_RESTART[comp]}
        if comp == "engine":
            ref["recording_gap"] = True
        existing = repos.online.get_target(target)
        if existing is not None and existing.hashes.get("sha256") != ref["sha256"]:
            raise PublishError(f"{target} ya existe con otro contenido: cambia la versión del componente")
        comps[comp] = ref
        new_files[target] = path
    inherited = []
    if prev is not None:
        for comp, ref in prev[1].get("components", {}).items():
            if comp not in comps:
                comps[comp] = ref
                inherited.append(comp)
    missing = [c for c in ("runtime", "app") if c not in comps]
    if missing:
        raise PublishError(f"Faltan componentes y no hay versión anterior de la que heredarlos: {missing}")
    prev_db = (prev[1].get("db") or {}).get("central_migration") if prev else None
    latest_mig, irreversible = migrations_after(prev_db)
    if irreversible and not opt.accept_irreversible:
        raise PublishError(f"Migraciones de la central NO reversibles ({irreversible}): repite con "
                           "--accept-irreversible (el rollback de la central restaurará el pg_dump)")
    db: dict[str, Any] = {}
    if latest_mig:
        db = {"central_migration": latest_mig, "store_requires_central_migration": latest_mig}
        if irreversible:
            db["irreversible"] = irreversible
    desc = build_descriptor(opt.version, dict(sorted(comps.items())), min_from=opt.min_from, security=opt.security,
                            severity=opt.severity, notes_es=opt.notes_es,
                            config_schema=opt.config_schema or default_config_schema(), db=db,
                            authenticode=opt.authenticode,
                            requires={"windows_build_min": opt.windows_build_min, "webview2_min": opt.webview2_min})
    released = utcnow_iso()
    for target, path in new_files.items():
        comp = target.split("/")[1]
        repos.add_target(target, path, {"kind": "component", "component": comp, "version": comps[comp]["version"]})
    repos.add_target(bundle_target(opt.version), desc, {"kind": "bundle", "version": opt.version, "released": released})
    if opt.channel:
        _set_channel_targets(repos, opt.channel, opt.version, paused=False)
    repos.commit(meta=opt.meta)
    return {"version": opt.version, "new_components": sorted(c.split("/")[1] for c in new_files),
            "inherited": sorted(inherited), "channel": opt.channel, "targets_version": repos.online.targets.signed.version,
            "previous": prev[0] if prev else None, "meta": opt.meta,
            "older_than_latest": bool(newest and Version.parse(newest[0]) > Version.parse(opt.version)),
            "windows_build_min": opt.windows_build_min}


def channel_doc(name: str, version: str, *, paused: bool) -> bytes:
    doc = {"schema": 1, "channel": name, "version": version, "paused": paused, "updated": utcnow_iso()}
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _set_channel_targets(repos: Repos, name: str, version: str, *, paused: bool) -> None:
    repos.add_target(channel_target(name), channel_doc(name, version, paused=paused),
                     {"kind": "channel", "channel": name, "version": version, "paused": paused})


def set_channel(repos: Repos, name: str, *, version: str | None = None, pause: bool | None = None,
                meta: str = "local") -> dict[str, Any]:
    if not re.match(r"^[a-z][a-z0-9-]{1,31}$", name):
        raise PublishError(f"Nombre de canal no válido: {name!r}")
    cur = repos.online.get_target(channel_target(name))
    cur_doc = json.loads(repos.online.read_target(channel_target(name))) if cur else None
    target_version = version or (cur_doc or {}).get("version")
    if not target_version:
        raise PublishError(f"El canal «{name}» no existe todavía: indica --version")
    if repos.online.get_target(bundle_target(target_version)) is None:
        raise PublishError(f"La {target_version} no está publicada")
    paused = pause if pause is not None else bool((cur_doc or {}).get("paused", False))
    _set_channel_targets(repos, name, target_version, paused=paused)
    repos.commit(meta=meta)
    return {"channel": name, "version": target_version, "paused": paused}


def publish_advisories(repos: Repos, table_file: Path, *, meta: str = "local") -> dict[str, Any]:
    from vms_updater.models import AdvisoryTableLite

    data = Path(table_file).read_bytes()
    try:
        table = AdvisoryTableLite.model_validate_json(data)
    except ValueError as exc:
        raise PublishError(f"La tabla de avisos no es válida: {exc}") from exc
    gen = table.generated_at.astimezone(timezone.utc)
    name = f"data/advisories-{gen.strftime('%Y%m%d')}.json"
    custom = {"kind": "data", "data": "advisories", "schema": 1,
              "generated_at": gen.replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    repos.add_target(name, data, custom)
    repos.commit(meta=meta)
    return {"target": name, "generated_at": custom["generated_at"], "advisories": len(table.advisories)}


def dry_run_copy(base: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="vms-release-dry-"))
    if Path(base).is_dir():
        shutil.copytree(base, tmp / "repo")
    else:
        (tmp / "repo").mkdir()
    return tmp / "repo"
