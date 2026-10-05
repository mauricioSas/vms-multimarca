"""`python -m tools.release` (PLAN-V2 §1.6, §1.7, §2.7 y criterio 3 de B4)."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from tools.release import site_token as st
from tools.release.__main__ import main as release_main
from tools.release.keys import Keyring, KeyringError, init_dev
from tools.release.package import PackageError, make_zip
from tools.release.publish import PublishError, PublishOptions, Repos, publish

from .conftest import ROOT, ReleaseFactory, install, serve_dir


def run_cli(*args: str) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = release_main(list(args))
    text = buf.getvalue()
    return rc, (json.loads(text) if text.strip().startswith(("{", "[")) else {})


def make_artifacts(base: Path, version: str = "2.0.0") -> Path:
    art = base / "art"
    for comp, files in {"app": {"app/x.py": b"print(1)", "bin/vmsctl.exe": b"MZ"},
                        "runtime": {"runtime/python.exe": b"MZ py"}}.items():
        src = base / "src" / comp
        for rel, data in files.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        make_zip(src, art / "components" / comp / f"{comp}-{version}.zip", component=comp)
    return art


def test_component_zip_is_reproducible_and_has_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    (src / "app" / "vms").mkdir(parents=True)
    (src / "app" / "vms" / "a.py").write_text("x = 1\n")
    (src / "bin").mkdir()
    (src / "bin" / "vmsctl.exe").write_bytes(b"MZ")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1760000000")
    h1 = make_zip(src, tmp_path / "a.zip", component="app")
    os.utime(src / "app" / "vms" / "a.py", (1, 1))                 # la fecha del archivo no influye
    h2 = make_zip(src, tmp_path / "b.zip", component="app")
    assert h1 == h2
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:
        names = zf.namelist()
        assert names[0] == "MANIFEST.sha256" and names[1:] == sorted(names[1:])
        manifest = zf.read("MANIFEST.sha256").decode()
        assert "app/vms/a.py" in manifest and "bin/vmsctl.exe" in manifest
    (src / "runtime").mkdir()
    (src / "runtime" / "x").write_text("x")
    with pytest.raises(PackageError, match="solo puede llevar"):
        make_zip(src, tmp_path / "c.zip", component="app")


def test_publish_dry_run_generates_repo_validated_by_ngclient(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    rc, _ = run_cli("--keys", str(keys), "keys", "init-dev")
    assert rc == 0
    art = make_artifacts(tmp_path)
    repo = tmp_path / "repo"
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "publish", "--version", "2.0.0",
                      "--artifacts", str(art), "--channel", "stable", "--dry-run", "--out", str(tmp_path / "dry"))
    assert rc == 0 and out["dry_run"] is True
    assert not repo.exists()                                            # --dry-run no toca el repositorio
    for mode in ("online", "offline"):
        v = out["verify"][mode]
        assert v["ok"] and v["bundles"] == {"2.0.0": ["app", "runtime"]}
        assert v["channels"] == {"stable": {"version": "2.0.0", "paused": False}}
        assert v["key_types"]["root"] == ["ecdsa-sha2-nistp256"]
        assert v["key_types"]["targets"] == ["ecdsa-sha2-nistp256"]
        assert v["key_types"]["snapshot"] == ["ed25519"] and v["key_types"]["timestamp"] == ["ed25519"]
    root = json.loads((tmp_path / "dry" / "online" / "metadata" / "1.root.json").read_text())
    assert root["signed"]["x-vms-env"] == "dev" and root["signed"]["roles"]["root"]["threshold"] == 2
    assert len(root["signed"]["roles"]["root"]["keyids"]) == 3
    off = json.loads((tmp_path / "dry" / "offline" / "metadata" / "1.root.json").read_text())
    assert off["signed"]["roles"]["snapshot"]["keyids"] == off["signed"]["roles"]["timestamp"]["keyids"]
    assert off["signed"]["roles"]["snapshot"]["keyids"] != root["signed"]["roles"]["snapshot"]["keyids"]


def test_full_cli_cycle_publish_channel_mirror_upload(tmp_path: Path) -> None:
    keys, repo = tmp_path / "keys", tmp_path / "repo"
    assert run_cli("--keys", str(keys), "keys", "init-dev")[0] == 0
    assert run_cli("--keys", str(keys), "--repo", str(repo), "init")[0] == 0
    art = make_artifacts(tmp_path)
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "publish", "--version", "2.0.0", "--artifacts",
                      str(art), "--channel", "pilot")
    assert rc == 0 and out["new_components"] == ["app", "runtime"]
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "channel", "stable", "--version", "2.0.0")
    assert rc == 0 and out == {"channel": "stable", "version": "2.0.0", "paused": False}
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "channel", "stable", "--pause")
    assert rc == 0 and out["paused"] is True
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "mirror", "--version", "2.0.0", "--out",
                      str(tmp_path / "usb"))
    assert rc == 0 and out["verify"]["ok"] and (tmp_path / "usb" / "LEEME.txt").is_file()
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "upload", "--to", str(tmp_path / "web"))
    assert rc == 0
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "verify")
    assert rc == 0 and out["online"]["ok"] and out["offline"]["ok"]
    rc, out = run_cli("verify", "--dir", str(tmp_path / "web"), "--mode", "online")
    assert rc == 0 and out["bundles"] == {"2.0.0": ["app", "runtime"]}
    rc, out = run_cli("--keys", str(keys), "--repo", str(repo), "sign-meta", "--timestamp-only")
    assert rc == 0 and out["mode"] == "online"
    assert run_cli("--keys", str(keys), "--repo", str(repo), "upload", "--to", "r2://vms-updates")[0] == 1


def test_publish_rules(tmp_path: Path, keyring: Keyring) -> None:
    repos = Repos.open(tmp_path / "repo", keyring, create=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PublishError, match="Faltan componentes"):
        publish(repos, PublishOptions(version="2.0.0", artifacts=empty))
    art = make_artifacts(tmp_path)
    publish(repos, PublishOptions(version="2.0.0", artifacts=art))
    with pytest.raises(PublishError, match="ya está publicada"):
        publish(repos, PublishOptions(version="2.0.0", artifacts=art))
    with pytest.raises(PublishError, match="no es mayor"):
        publish(repos, PublishOptions(version="1.9.0", artifacts=empty))
    out = publish(repos, PublishOptions(version="2.0.1", artifacts=empty))
    assert out["inherited"] == ["app", "runtime"] and out["new_components"] == []


def test_irreversible_central_migration_needs_explicit_ack(tmp_path: Path, keyring: Keyring,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.release.publish as pub
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0001_init.sql").write_text("-- reversible: yes\n")
    (mig / "0002_x.sql").write_text("-- reversible: no\nDROP TABLE x;\n")
    monkeypatch.setattr(pub, "MIGRATIONS_DIR", mig)
    repos = Repos.open(tmp_path / "repo", keyring, create=True)
    art = make_artifacts(tmp_path)
    with pytest.raises(PublishError, match="NO reversibles"):
        publish(repos, PublishOptions(version="2.0.0", artifacts=art))
    publish(repos, PublishOptions(version="2.0.0", artifacts=art, accept_irreversible=True))
    desc = json.loads(repos.online.read_target("bundles/vms-2.0.0.json"))
    assert desc["db"] == {"central_migration": "0002", "store_requires_central_migration": "0002",
                          "irreversible": ["0002"]}


def test_keyring_rules(tmp_path: Path) -> None:
    kr = init_dev(tmp_path / "k")
    with pytest.raises(KeyringError, match="Ya hay"):
        init_dev(tmp_path / "k")
    e = kr.keys["dev-targets"]
    e.uri = "envpem:ALGO"
    with pytest.raises(KeyringError, match="nunca desde una variable"):
        kr.validate()
    e.uri = "file2:dev-targets.pem"
    kr.keys["prod-x"] = kr.keys.pop("dev-snapshot")
    kr.keys["prod-x"].name = "prod-x"
    with pytest.raises(KeyringError, match="dev-"):
        kr.validate()
    # por defecto, las claves privadas viven fuera del repositorio de código (~/.vms-dev-keys) y ningún
    # archivo de claves está versionado
    from tools.release.keys import DEFAULT_DIR
    assert not str(DEFAULT_DIR.resolve()).startswith(str(ROOT))
    assert not [p for p in ROOT.glob("*/**/*.pem") if ".tmp" not in p.parts and "node_modules" not in p.parts]


def test_repo_and_keyring_env_cannot_mix(tmp_path: Path, keyring: Keyring) -> None:
    Repos.open(tmp_path / "repo", keyring, create=True)
    keyring.env = "prod"
    from tools.release.tuf_repo import RepoError
    with pytest.raises(RepoError, match="no se mezclan"):
        Repos.open(tmp_path / "repo", keyring)


def test_snapshot_timestamp_from_env_pem_for_ci(tmp_path: Path, keyring: Keyring, monkeypatch: pytest.MonkeyPatch) -> None:
    """publish-meta.yml / timestamp.yml: snapshot y timestamp desde un secreto, nunca root ni targets."""
    repos = Repos.open(tmp_path / "repo", keyring, create=True)
    for name in ("dev-snapshot", "dev-timestamp"):
        pem = (keyring.path / f"{name}.pem").read_text()
        monkeypatch.setenv(f"VMS_TUF_{name.split('-')[1].upper()}_PEM", pem)
        keyring.keys[name].uri = f"envpem:VMS_TUF_{name.split('-')[1].upper()}_PEM"
        (keyring.path / f"{name}.pem").unlink()
    keyring.save()
    kr = Keyring.load(keyring.path)
    repos.online.write_snapshot_timestamp(kr, timestamp_only=True)
    assert repos.online.timestamp.signed.version == 2


def test_site_tokens_local_kv(tmp_path: Path) -> None:
    kv = st.LocalKV(tmp_path / "kv.json")
    tok = st.add(kv, "covert", "S0042")
    assert tok.startswith("covert.") and len(tok) > 40
    raw = json.loads((tmp_path / "kv.json").read_text())
    assert list(raw) == [f"covert:{st.token_hash(tok)}"] and tok not in json.dumps(raw)
    assert st.list_sites(kv, "covert")[0]["active"] is True
    assert st.revoke(kv, "covert", "S0042") == 1
    assert st.list_sites(kv, "covert")[0]["active"] is False
    st.add(kv, "otro", "S1")
    assert st.revoke_client(kv, "covert") == 1 and st.list_sites(kv, "covert") == []
    with pytest.raises(st.TokenError):
        st.add(kv, "Covert!", "S1")
    out = tmp_path / "secrets.json"
    rc, res = run_cli("site-token", "add", "--client", "covert", "--site", "S7", "--kv-file", str(tmp_path / "kv.json"),
                      "--out", str(out))
    assert rc == 0 and "token" not in res
    assert json.loads(out.read_text())["update_token"].startswith("covert.")
    if os.name != "nt":
        assert (out.stat().st_mode & 0o777) == 0o600
    assert run_cli("site-token", "list", "--client", "covert")[0] == 1      # sin cuenta de Cloudflare: error claro


def test_worker_token_is_sent_only_to_targets(tmp_path: Path, factory: ReleaseFactory, repo_url: str) -> None:
    """El token de sede viaja solo en las peticiones a targets/ (nunca a metadata/ ni en los registros)."""
    import http.server
    import threading
    seen: list[tuple[str, str | None]] = []
    root = factory.repo_dir / "online"

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            super().__init__(*a, directory=str(root), **k)

        def do_GET(self) -> None:  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization")))
            super().do_GET()

        def log_message(self, *a):  # type: ignore[no-untyped-def]
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        factory.release("2.1.0", comps=("app",))
        site = install(tmp_path / "t", factory, f"http://127.0.0.1:{srv.server_address[1]}/")
        assert site.engine(token="covert.TOKEN").check().result == "update_ok"
    finally:
        srv.shutdown()
        srv.server_close()
    assert any(p.startswith("/targets/") and a == "Bearer covert.TOKEN" for p, a in seen)
    assert all(a is None for p, a in seen if p.startswith("/metadata/"))


SOFTHSM_LIBS = ["/usr/lib/softhsm/libsofthsm2.so", "/opt/homebrew/lib/softhsm/libsofthsm2.so",
                "/usr/local/lib/softhsm/libsofthsm2.so"]


@pytest.mark.skipif(not shutil.which("softhsm2-util") or not any(Path(p).exists() for p in SOFTHSM_LIBS),
                    reason="SoftHSM2 no está instalado (el job B4 de CI lo instala)")
def test_publish_dry_run_with_pkcs11_softhsm(tmp_path: Path) -> None:
    lib = next(p for p in SOFTHSM_LIBS if Path(p).exists())
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    conf = tmp_path / "softhsm2.conf"
    conf.write_text(f"directories.tokendir = {tokens}\nobjectstore.backend = file\n")
    env = dict(os.environ, SOFTHSM2_CONF=str(conf))
    subprocess.run(["softhsm2-util", "--init-token", "--free", "--label", "vms-dev", "--pin", "1234",
                    "--so-pin", "4321"], env=env, check=True, capture_output=True)
    env.update({"PYKCS11LIB": lib, "VMS_RELEASE_PIN": "1234",
                "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "updater")])})
    keys = tmp_path / "keys"
    p = subprocess.run([sys.executable, "-m", "tools.release", "--keys", str(keys), "keys", "init-dev",
                        "--pkcs11-lib", lib, "--pin", "1234"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, p.stderr
    desc = json.loads(p.stdout)
    assert desc["keys"]["dev-targets"]["where"] == "hsm" and desc["keys"]["dev-root-1"]["where"] == "hsm"
    assert not (keys / "dev-targets.pem").exists()                      # la clave no sale del token
    art = make_artifacts(tmp_path)
    p = subprocess.run([sys.executable, "-m", "tools.release", "--keys", str(keys), "--repo", str(tmp_path / "repo"),
                        "publish", "--version", "2.0.0", "--artifacts", str(art), "--channel", "stable",
                        "--dry-run"], env=env, capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    for mode in ("online", "offline"):
        assert out["verify"][mode]["ok"]
        assert out["verify"][mode]["key_types"]["targets"] == ["ecdsa-sha2-nistp256"]
        assert out["verify"][mode]["key_types"]["root"] == ["ecdsa-sha2-nistp256"]


def test_serve_dir_helper(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("hola")
    import urllib.request
    with serve_dir(tmp_path) as url:
        assert urllib.request.urlopen(url + "x.txt").read() == b"hola"
