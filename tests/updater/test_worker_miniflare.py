"""Worker de Cloudflare probado con Miniflare (criterio 4 de B4) y actualización de punta a punta a través de él.

Necesita Node ≥ 22.18 y `npm ci` en `infra/update-worker` (lo hace el job B4 de CI). Sin eso, se omite.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tools.release import site_token as st

from .conftest import ROOT, ReleaseFactory, install

WORKER = ROOT / "infra" / "update-worker"
NODE = shutil.which("node") or (str(Path.home() / ".local/bin/node") if (Path.home() / ".local/bin/node").exists() else None)

pytestmark = pytest.mark.skipif(not NODE or not (WORKER / "node_modules" / "miniflare").is_dir(),
                                reason="Falta Node o «npm ci» en infra/update-worker")


def test_worker_unit_tests_with_miniflare() -> None:
    p = subprocess.run([NODE, "--test", *sorted(str(x) for x in (WORKER / "test").glob("*.test.mjs"))],  # type: ignore[list-item]
                       cwd=WORKER, capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
    for name in ("sin token → 401", "token revocado → 401", "otro cliente → 403", "metadatos son públicos"):
        assert name in p.stdout


@pytest.fixture
def worker(factory: ReleaseFactory, tmp_path: Path) -> Iterator[tuple[str, dict[str, str]]]:
    factory.release("2.1.0", comps=("app",))
    kv = st.LocalKV(tmp_path / "kv.json")
    tokens = {"ok": st.add(kv, "covert", "S0042"), "revoked": st.add(kv, "covert", "S0007"),
              "other": st.add(kv, "otro", "X1")}
    st.revoke(kv, "covert", "S0007")
    proc = subprocess.Popen([NODE, "scripts/dev-serve.mjs", "--repo", str(factory.repo_dir / "online"),  # type: ignore[list-item]
                             "--kv", str(tmp_path / "kv.json")], cwd=WORKER, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line, proc.stderr.read() if proc.stderr else ""
        yield json.loads(line)["url"], tokens
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_updater_downloads_through_worker_with_site_token(worker: tuple[str, dict[str, str]], tmp_path: Path,
                                                          factory: ReleaseFactory, repo_url: str) -> None:
    url, tokens = worker
    site = install(tmp_path / "tienda", factory, repo_url)
    out = site.engine(source_url=url + "covert/", token=tokens["ok"]).check()
    assert out.result == "update_ok", out.message_es
    assert site.pointer().active == "2.1.0"


@pytest.mark.parametrize("which,expect", [("revoked", "401"), ("other", "403"), (None, "401")])
def test_updater_rejected_by_worker(worker: tuple[str, dict[str, str]], tmp_path: Path, factory: ReleaseFactory,
                                    repo_url: str, which: str | None, expect: str) -> None:
    url, tokens = worker
    site = install(tmp_path / "tienda", factory, repo_url)
    out = site.engine(source_url=url + "covert/", token=tokens[which] if which else None).check()
    assert out.result == "error" and expect in out.message_es and "token" in out.message_es
    assert site.pointer().active == "2.0.0"
