"""Fixtures compartidas por todas las pruebas (ver docs/CONTRATO.md §9).

Aislamiento garantizado:
  - Ninguna prueba toca el llavero real del sistema (keyring en memoria).
  - Ninguna prueba lee el .env del desarrollador (se limpian las variables VMS_*).
  - Los temporales de pytest van a <proyecto>/.tmp/pytest (no a /tmp del sistema).
  - Todos los puertos se piden libres al sistema: varias sesiones de pytest pueden correr a la vez.
  - PostgreSQL: un servidor pgserver por sesión y una BASE NUEVA por prueba (clonada de una
    plantilla ya migrada), así ninguna prueba ve datos de otra.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / ".tmp"

# --- entorno de proceso (antes de importar nada del proyecto) -------------------------------
for _k in [k for k in os.environ if k.startswith("VMS_") and not k.startswith(("VMS_TEST_", "VMS_MEDIAMTX_BIN"))]:
    del os.environ[_k]
os.environ["VMS_ENV_FILE"] = str(TMP_ROOT / "no-existe.env")
os.environ["PYTHON_KEYRING_BACKEND"] = "vms.core.credentials.MemoryKeyring"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")  # Chromium instalado dentro del venv
(TMP_ROOT / "pytest").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(TMP_ROOT / "pytest"))

from vms.core.config_store import ConfigRepository, ConfigStore  # noqa: E402
from vms.core.credentials import CredentialStore, EncryptedFileBackend  # noqa: E402
from vms.core.paths import AppPaths  # noqa: E402
from vms.core.settings import VmsSettings  # noqa: E402


# =========================================================================== utilidades
def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def free_port() -> Callable[[], int]:
    return get_free_port


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


# =========================================================================== binarios externos
@pytest.fixture(scope="session")
def mediamtx_bin() -> str:
    exe = "mediamtx.exe" if sys.platform == "win32" else "mediamtx"
    for c in (os.environ.get("VMS_MEDIAMTX_BIN"), str(ROOT / "bin" / exe), shutil.which("mediamtx")):
        if c and Path(c).is_file():
            return c
    pytest.skip("Falta MediaMTX: ejecuta «python -m tools.fetch_mediamtx»")


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    # Sin rutas fijas de Homebrew (PLAN-V2 §5): VMS_TEST_FFMPEG o el PATH, igual en Windows, Linux y macOS.
    for c in (os.environ.get("VMS_TEST_FFMPEG"), shutil.which("ffmpeg")):
        if c and Path(c).is_file():
            return c
    pytest.skip("Falta ffmpeg (solo para generar flujos de prueba); define VMS_TEST_FFMPEG")


@pytest.fixture(scope="session")
def ffprobe_bin(ffmpeg_bin: str) -> str:
    probe = Path(ffmpeg_bin).with_name("ffprobe" + (".exe" if sys.platform == "win32" else ""))
    if probe.is_file():
        return str(probe)
    found = os.environ.get("VMS_TEST_FFPROBE") or shutil.which("ffprobe")
    if found:
        return found
    pytest.skip("Falta ffprobe (PATH o VMS_TEST_FFPROBE)")


@pytest.fixture(scope="session")
def people_video() -> Path:
    """Vídeo H.264 baseline con personas caminando (tests/assets, no versionado)."""
    for name in ("people-walking-h264.mp4", "people-walking.mp4"):
        p = ROOT / "tests" / "assets" / name
        if p.is_file():
            return p
    pytest.skip("Falta el vídeo de prueba: ejecuta «python -m tools.download_test_assets»")


# =========================================================================== núcleo
@pytest.fixture
def app_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppPaths:
    """Carpeta de datos aislada para la prueba (también exportada como VMS_DATA_DIR)."""
    base = tmp_path / "data"
    monkeypatch.setenv("VMS_DATA_DIR", str(base))
    return AppPaths(base).ensure()


@pytest.fixture
def settings(app_paths: AppPaths) -> VmsSettings:
    """Ajustes de prueba: puertos de MediaMTX libres y token interno fijo."""
    return VmsSettings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=app_paths.base,
        site_id="site-test",
        http_host="127.0.0.1",
        http_port=get_free_port(),
        mtx_rtsp_address=f"127.0.0.1:{get_free_port()}",
        mtx_webrtc_address=f"127.0.0.1:{get_free_port()}",
        mtx_webrtc_ice_udp=f":{get_free_port()}",
        mtx_webrtc_ice_tcp="",
        mtx_api_address=f"127.0.0.1:{get_free_port()}",
        mtx_playback_address=f"127.0.0.1:{get_free_port()}",
        mtx_metrics_address=f"127.0.0.1:{get_free_port()}",
        internal_token="token-interno-de-pruebas",
        kiosk_token="token-kiosco-de-pruebas",
        credential_backend="file",
    )


@pytest.fixture
def credential_store(app_paths: AppPaths) -> CredentialStore:
    key = EncryptedFileBackend.load_or_create_key(app_paths.secrets_dir)
    return CredentialStore(EncryptedFileBackend(app_paths.secrets_dir / "credentials.enc", key))


@pytest.fixture
def config_repo(app_paths: AppPaths) -> ConfigRepository:
    return ConfigRepository(ConfigStore(app_paths.config_file))


# =========================================================================== PostgreSQL
@pytest.fixture(scope="session")
def pg_server_dsn() -> Iterator[str]:
    """DSN de un servidor PostgreSQL de pruebas: VMS_TEST_PG_DSN o pgserver embebido."""
    explicit = os.environ.get("VMS_TEST_PG_DSN")
    if explicit:
        yield explicit
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("pgserver no está instalado y no hay VMS_TEST_PG_DSN")
    pgdata = TMP_ROOT / f"pg-{os.getpid()}"
    srv = pgserver.get_server(pgdata, cleanup_mode="delete")
    try:
        yield srv.get_uri()
    finally:
        srv.cleanup()


def _dsn_with_db(dsn: str, dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(dsn)
    params["dbname"] = dbname
    return make_conninfo(**params)


@pytest.fixture(scope="session")
def pg_template(pg_server_dsn: str) -> Iterator[str]:
    """Nombre de una base plantilla con todas las migraciones aplicadas."""
    import psycopg
    from vms.db.migrate import apply_migrations

    name = f"vms_tpl_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(pg_server_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    apply_migrations(_dsn_with_db(pg_server_dsn, name))
    yield name
    with psycopg.connect(pg_server_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def pg_dsn(pg_server_dsn: str, pg_template: str) -> Iterator[str]:
    """Base de datos NUEVA y ya migrada para esta prueba. Se borra al terminar."""
    import psycopg

    name = f"vms_t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{pg_template}"')
    try:
        yield _dsn_with_db(pg_server_dsn, name)
    finally:
        with psycopg.connect(pg_server_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def pg_empty_dsn(pg_server_dsn: str) -> Iterator[str]:
    """Base de datos NUEVA y VACÍA (sin migraciones), para probar las propias migraciones."""
    import psycopg

    name = f"vms_e_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _dsn_with_db(pg_server_dsn, name)
    finally:
        with psycopg.connect(pg_server_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# =========================================================================== simulador de cámaras
@pytest.fixture
def camsim_factory(tmp_path: Path, mediamtx_bin: str, ffmpeg_bin: str) -> Iterator[Callable[..., object]]:
    """Crea simuladores a medida; todos se paran al acabar la prueba.

        sim = camsim_factory([SimDevice("hik1", "hikvision", channels=2)])
    """
    from tools.camsim.simulator import CameraSimulator, SimDevice

    created: list[CameraSimulator] = []

    def make(devices: list[SimDevice] | None = None, **kwargs: object) -> CameraSimulator:
        devs = devices or [SimDevice("hik1", "hikvision", channels=2), SimDevice("dah1", "dahua", channels=2)]
        sim = CameraSimulator(devs, tmp_path / f"camsim{len(created)}", mediamtx_bin=mediamtx_bin,
                              ffmpeg_bin=ffmpeg_bin, **kwargs)  # type: ignore[arg-type]
        created.append(sim)
        return sim.start()

    yield make
    for sim in created:
        sim.stop()


@pytest.fixture
def camsim(camsim_factory: Callable[..., object]) -> object:
    """Simulador por defecto: hik1 (NVR Hikvision, 2 canales) + dah1 (NVR Dahua, 2 canales)."""
    return camsim_factory()


# =========================================================================== mocks HTTP de fabricantes
@pytest.fixture
def hik_mock() -> object:
    from tools.mocks.hikvision import HikvisionMock
    return HikvisionMock()


@pytest.fixture
def dahua_mock() -> object:
    from tools.mocks.dahua import DahuaMock
    return DahuaMock()


@pytest.fixture
def mock_server() -> Iterator[Callable[[object], object]]:
    """Sirve un mock ASGI en un puerto real: srv = mock_server(hik_mock.app); srv.base_url."""
    from tools.mocks.server import MockHttpServer

    servers: list[MockHttpServer] = []

    def serve(app: object) -> MockHttpServer:
        srv = MockHttpServer(app).start()
        servers.append(srv)
        return srv

    yield serve
    for srv in servers:
        srv.stop()


@pytest.fixture
async def asgi_client() -> AsyncIterator[Callable[..., object]]:
    """Cliente httpx sin red contra un mock ASGI: client = asgi_client(app, auth=httpx.DigestAuth(u, p))."""
    import httpx

    clients: list[httpx.AsyncClient] = []

    def make(app: object, base_url: str = "http://device.local", **kwargs: object) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url, **kwargs)  # type: ignore[arg-type]
        clients.append(c)
        return c

    yield make
    for c in clients:
        await c.aclose()
