"""Infraestructura de las pruebas del actualizador (PLAN-V2 §4.4): repositorio TUF temporal con claves de
desarrollo generadas en la prueba, servido por HTTP local, y un «equipo» simulado (carpetas de instalación
y de datos) con la 2.0.0 instalada como lo haría el instalador.
"""
from __future__ import annotations

import contextlib
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "updater") not in sys.path:
    sys.path.insert(0, str(ROOT / "updater"))

import tools.release  # noqa: E402,F401 - añade updater/ al path también para tools.release
from tools.release.keys import Keyring, init_dev  # noqa: E402
from tools.release.package import make_zip  # noqa: E402
from tools.release.publish import PublishOptions, Repos, publish  # noqa: E402
from tools.release.verify import serve  # noqa: E402
from vms_updater._atomic import atomic_write_json  # noqa: E402
from vms_updater.client import TufClient, parse_source  # noqa: E402
from vms_updater.engine import Engine, EngineDeps  # noqa: E402
from vms_updater.journal import FaultHook, JournalStore  # noqa: E402
from vms_updater.layout import Layout  # noqa: E402
from vms_updater.models import ActivePointer, Journal, JournalStep, ReleaseDescriptor, bundle_target  # noqa: E402
from vms_updater.pointer import PointerStore  # noqa: E402
from vms_updater.stage import stage_version  # noqa: E402
from vms_updater.system import FakeSystem  # noqa: E402

from .doubles.fake_backend import FastHealth  # noqa: E402
from .doubles.vmsctl_double import DoubleServices, do_services  # noqa: E402

SERVICES = ["VMSEngine", "VMSBackend", "VMSHeartbeat"]
IN_WINDOW = datetime(2026, 11, 20, 1, 30).astimezone()
OUT_OF_WINDOW = datetime(2026, 11, 20, 14, 0).astimezone()


@pytest.fixture(scope="session")
def _session_keys(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("keys") / "dev"
    init_dev(d)
    return d


@pytest.fixture
def keyring(_session_keys: Path, tmp_path: Path) -> Keyring:
    d = tmp_path / "keys"
    shutil.copytree(_session_keys, d)
    return Keyring.load(d)


class ReleaseFactory:
    def __init__(self, base: Path, kr: Keyring) -> None:
        self.base = base
        self.kr = kr
        self.repo_dir = base / "repo"
        self.repos = Repos.open(self.repo_dir, kr, create=True)

    def component(self, art: Path, comp: str, cver: str, files: dict[str, bytes]) -> Path:
        src = self.base / "src" / f"{comp}-{cver}"
        shutil.rmtree(src, ignore_errors=True)
        for rel, data in files.items():
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        out = art / "components" / comp / f"{comp}-{cver}.zip"
        make_zip(src, out, component=comp)
        return out

    def release(self, version: str, *, comps: tuple[str, ...] = ("app",), broken: bool = False,
                fewer_cameras: bool = False, channel: str | None = "stable", updater_files: dict[str, bytes] | None = None,
                **opts: Any) -> dict[str, Any]:
        art = self.base / f"art-{version}"
        shutil.rmtree(art, ignore_errors=True)
        for comp in comps:
            if comp == "app":
                files = {"app/VERSION.txt": f"{version}\n".encode(),
                         "bin/vmsctl.exe": b"MZ vmsctl " + version.encode(),
                         "THIRD_PARTY_NOTICES.txt": b"avisos\n"}
                if broken:
                    files["app/BROKEN"] = b"1"
                if fewer_cameras:
                    files["app/FEWER_CAMERAS"] = b"1"
                self.component(art, "app", version, files)
            elif comp == "runtime":
                self.component(art, "runtime", f"3.12.10-{version}", {"runtime/python.txt": b"python " + version.encode(),
                                                                      "runtime/Lib/site.txt": b"x"})
            elif comp == "engine":
                self.component(art, "engine", f"1.21.1-{version}", {"engine/mediamtx.exe": b"MZ mediamtx " + version.encode(),
                                                                    "engine/MEDIAMTX-LICENSE.txt": b"MIT"})
            elif comp == "viewer":
                self.component(art, "viewer", version, {"viewer/VMS.exe": b"MZ visor " + version.encode()})
            elif comp == "models":
                self.component(art, "models", f"rfdetr-{version}", {"models/rfdetr-nano.xml": b"<xml/>" + version.encode()})
        if updater_files is not None:
            self.component(art, "updater", version, updater_files)
        return publish(self.repos, PublishOptions(version=version, artifacts=art, channel=channel, **opts))

    def descriptor(self, version: str) -> tuple[ReleaseDescriptor, bytes]:
        raw = self.repos.online.read_target(bundle_target(version))
        return ReleaseDescriptor.model_validate_json(raw), raw


@pytest.fixture
def factory(tmp_path: Path, keyring: Keyring) -> ReleaseFactory:
    f = ReleaseFactory(tmp_path / "pub", keyring)
    f.release("2.0.0", comps=("runtime", "app", "engine", "viewer"))
    return f


@pytest.fixture
def repo_url(factory: ReleaseFactory) -> Iterator[str]:
    with serve(factory.repo_dir / "online") as url:
        yield url


@dataclass
class Site:
    layout: Layout
    factory: ReleaseFactory
    url: str
    mode: str = "online"
    system: FakeSystem = field(default_factory=FakeSystem)
    health: FastHealth | None = None
    migrations: list[str] = field(default_factory=list)

    def engine(self, *, now_local: datetime = IN_WINDOW, fault_hook: FaultHook | None = None, slot: str | None = None,
               source_url: str | None = None, now_utc: Callable[[], datetime] | None = None,
               verifier: Any = None, require_authenticode: bool = False, token: str | None = None) -> Engine:
        layout = self.layout
        trusted = layout.trusted_root_file(self.mode).read_bytes()
        url = source_url or self.url
        source = parse_source(url, mode=self.mode, token=token)

        def client_factory() -> TufClient:
            return TufClient(source, metadata_dir=layout.tuf_metadata_dir, targets_dir=layout.tuf_targets_dir,
                             trusted_root=trusted)

        def migrate(version: str, *, central: bool = False) -> None:
            self.migrations.append(version)
            cfg = layout.config_dir / "config.json"
            import json
            doc = json.loads(cfg.read_text(encoding="utf-8"))
            doc["migrated_by"] = version
            atomic_write_json(cfg, doc)

        self.health = FastHealth(layout)
        kwargs: dict[str, Any] = {}
        if now_utc is not None:
            kwargs["now_utc"] = now_utc
        deps = EngineDeps(layout=layout, client_factory=client_factory, services=DoubleServices(layout),
                          health=self.health, system=self.system, migrate=migrate, verifier=verifier,
                          dev_root=True, require_authenticode=require_authenticode,
                          now_local=lambda: now_local, fault_hook=fault_hook, slot=slot, **kwargs)
        return Engine(deps)

    # ---- consultas
    def pointer(self) -> ActivePointer:
        p = PointerStore(self.layout.pointer_file).read()
        assert p is not None
        return p

    def journal(self) -> Journal | None:
        return JournalStore(self.layout.journal_file).read()

    def running(self) -> dict[str, str]:
        from .doubles.vmsctl_double import load_state
        return dict(load_state(self.layout)["running"])

    def config_bytes(self) -> bytes:
        return (self.layout.config_dir / "config.json").read_bytes()


def install(base: Path, factory: ReleaseFactory, url: str, *, version: str = "2.0.0", mode: str = "online") -> Site:
    """Lo que deja el instalador: versions/<X>, puntero, diario con last_good, root de confianza, datos."""
    layout = Layout(install=base / "install", data=base / "data").ensure()
    desc, raw = factory.descriptor(version)
    repo = factory.repos.online
    zips = {c: repo.target_file_path(ref.target, ref.sha256) for c, ref in desc.components.items() if c != "updater"}
    stage_version(versions_dir=layout.versions_dir, descriptor=desc, descriptor_bytes=raw, zips=zips,
                  current_version=None, current_descriptor=None)
    PointerStore(layout.pointer_file).write(ActivePointer(active=version))
    now = int(time.time())
    JournalStore(layout.journal_file).write(Journal(state="good", last_good=version, to=version,
                                                    steps=[JournalStep(state="good", started_unix=now, done_unix=now)]))
    for m in ("online", "offline"):
        dst = layout.trusted_root_file(m)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(factory.repo_dir / m / "metadata" / "1.root.json", dst)
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(layout.config_dir / "config.json", {"version": 2, "devices": [], "cameras": [],
                                                          "settings": {"site": {"name": "Tienda 1"}}})
    atomic_write_json(layout.config_dir / "users.json", {"users": []})
    layout.secrets_dir.mkdir(parents=True, exist_ok=True)
    (layout.secrets_dir / "internal.token").write_text("tok-interno", encoding="utf-8")
    atomic_write_json(layout.local_config_file, {"channel": "stable", "services": SERVICES,
                                                 "inno_app_id": "{VMS-TEST}"})
    do_services(layout, "start", SERVICES)
    return Site(layout=layout, factory=factory, url=url, mode=mode)


@pytest.fixture
def site(tmp_path: Path, factory: ReleaseFactory, repo_url: str) -> Site:
    return install(tmp_path / "site", factory, repo_url)


def crash_at(state: str, phase: str) -> FaultHook:
    from vms_updater.journal import SimulatedCrash

    def hook(s: str, p: str) -> None:
        if s == state and p == phase:
            raise SimulatedCrash(f"{s}:{p}")

    return hook


@contextlib.contextmanager
def serve_dir(path: Path) -> Iterator[str]:
    with serve(path) as url:
        yield url
