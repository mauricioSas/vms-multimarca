"""Regresión (hallazgo «el backend solo sirve HTTP en 0.0.0.0»): con certificado, la web va por
HTTPS hacia la LAN, la cookie de sesión lleva Secure y el HTTP sin cifrar solo escucha en
127.0.0.1 (kiosco, analítica y agente del mismo PC)."""
from __future__ import annotations

import ssl
import sys
import time
from pathlib import Path

import httpx
import pytest
from cryptography import x509

from tests.api.test_main_process import _env, _start, _stop, _wait_health
from tests.conftest import get_free_port
from vms.api.serve import build_configs
from vms.core.settings import VmsSettings
from vms.core.tls import create_self_signed


def test_self_signed_certificate_has_names_and_ips(tmp_path: Path) -> None:
    files = create_self_signed(tmp_path / "tls", ["pc-control"], ["192.168.1.20"])
    cert = x509.load_pem_x509_certificate(files.cert.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "pc-control" in san.get_values_for_type(x509.DNSName)
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    assert {"192.168.1.20", "127.0.0.1"} <= ips
    assert b"PRIVATE KEY" in files.key.read_bytes()
    if sys.platform != "win32":
        assert files.key.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        create_self_signed(tmp_path / "bad", [], ["no-es-una-ip"])


def test_server_configs() -> None:
    plain = VmsSettings(_env_file=None, http_host="0.0.0.0")  # type: ignore[call-arg]
    http, https = build_configs(object(), plain)
    assert (http.host, https) == ("0.0.0.0", None)

    tls = VmsSettings(_env_file=None, http_host="0.0.0.0", tls_cert_file=Path("c.crt"),  # type: ignore[call-arg]
                      tls_key_file=Path("c.key"), https_port=9443)
    http, https = build_configs(object(), tls)
    assert http.host == "127.0.0.1"           # sin cifrar: solo el propio PC
    assert https is not None and (https.host, https.port, https.lifespan) == ("0.0.0.0", 9443, "off")
    with pytest.raises(ValueError):
        VmsSettings(_env_file=None, tls_cert_file=Path("c.crt"))  # type: ignore[call-arg]


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.needs_mediamtx
def test_backend_serves_https_with_secure_cookie(tmp_path: Path, mediamtx_bin: str) -> None:
    data, log = tmp_path / "data", tmp_path / "backend.log"
    files = create_self_signed(tmp_path / "tls", ["vms-test"], [])
    env = _env(data, mediamtx_bin)
    https_port = get_free_port()
    env.update({"VMS_TLS_CERT_FILE": str(files.cert), "VMS_TLS_KEY_FILE": str(files.key),
                "VMS_HTTPS_PORT": str(https_port)})
    proc = _start(env, log)
    try:
        _wait_health(env["VMS_HTTP_PORT"], proc, log)
        ctx = ssl.create_default_context(cafile=str(files.cert))
        base = f"https://127.0.0.1:{https_port}"
        deadline = time.monotonic() + 15
        while True:
            try:
                r = httpx.get(f"{base}/api/health", verify=ctx, timeout=3)
                break
            except httpx.ConnectError:
                assert time.monotonic() < deadline, log.read_text(errors="replace")[-1500:]
                time.sleep(0.3)
        assert r.status_code == 200
        r = httpx.post(f"{base}/api/auth/login", verify=ctx, headers={"X-Requested-With": "vms"},
                       json={"username": "admin", "password": "Admin#12345"})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "vms_session=" in cookie and "secure" in cookie and "httponly" in cookie
        # Por HTTP (127.0.0.1) sigue funcionando y la cookie NO lleva Secure (no se enviaría)
        r = httpx.post(f"http://127.0.0.1:{env['VMS_HTTP_PORT']}/api/auth/login", headers={"X-Requested-With": "vms"},
                       json={"username": "admin", "password": "Admin#12345"})
        assert r.status_code == 200 and "secure" not in r.headers["set-cookie"].lower()
        _stop(proc, log)
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"{base}/api/health", verify=ctx, timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert "Traceback" not in log.read_text(encoding="utf-8", errors="replace")


def test_plain_http_on_lan_logs_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import asyncio

    from vms.api import serve as serve_mod

    class _Stop:
        def __init__(self, config: object) -> None:
            self.config, self.started, self.should_exit = config, True, False

        async def serve(self) -> None:
            return None

    settings = VmsSettings(_env_file=None, http_host="0.0.0.0", http_port=get_free_port())  # type: ignore[call-arg]
    mp = pytest.MonkeyPatch()
    mp.setattr(serve_mod.uvicorn, "Server", _Stop)
    try:
        with caplog.at_level("WARNING", logger="vms.api.serve"):
            assert asyncio.run(serve_mod.serve(object(), settings)) is True
    finally:
        mp.undo()
    assert "sin cifrar" in caplog.text
