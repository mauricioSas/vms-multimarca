"""Fixtures compartidas por todas las pruebas (ver docs/CONTRATO.md §9).

Aislamiento garantizado:
  - Ninguna prueba toca el llavero real del sistema (keyring en memoria).
  - Ninguna prueba lee un .env que no haya creado ella: se borran las variables VMS_* (salvo VMS_TEST_* y
    VMS_MEDIAMTX_BIN*) y VMS_ENV_FILE_ONLY=1 deja fuera el .env del repositorio y el del producto
    instalado (C:\\ProgramData\\VMSMultimarca\\.env, que sin ser administrador da «Permission denied»).
  - Ninguna prueba toca la instalación real (%ProgramData%\\VMSMultimarca, %ProgramFiles%\\VMSMultimarca y las
    carpetas anotadas en HKLM\\SOFTWARE\\VMSMultimarca): un vigilante de auditoría hace fallar la prueba que
    abra, liste, cree o borre algo ahí, o que lance un programa cuya línea de órdenes las nombre. Se salta solo
    en el e2e del instalador (tests/windows/e2e y tests/windows/test_update_*.py).
  - Los temporales de pytest van a <proyecto>/.tmp/pytest (no a /tmp del sistema).
  - Todos los puertos se piden libres al sistema: varias sesiones de pytest pueden correr a la vez.
  - PostgreSQL: un servidor pgserver por sesión y una BASE NUEVA por prueba (clonada de una
    plantilla ya migrada), así ninguna prueba ve datos de otra. En Windows, con zonas horarias
    (tools/pgserver_tz.py) y con el bucle de eventos que acepta psycopg asíncrono donde hace falta.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import sys
import uuid
import warnings
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / ".tmp"

# Variables que solo tienen sentido dentro de las pruebas: nunca se pasan al producto real (el e2e del
# instalador lanza Setup.exe y vmsctl con el entorno de pytest; ver tests/windows/harness.py).
TEST_ONLY_ENV = ("VMS_ENV_FILE", "VMS_ENV_FILE_ONLY", "PYTHON_KEYRING_BACKEND")

# --- entorno de proceso (antes de importar nada del proyecto) -------------------------------
for _k in [k for k in os.environ if k.startswith("VMS_") and not k.startswith(("VMS_TEST_", "VMS_MEDIAMTX_BIN"))]:
    del os.environ[_k]
os.environ["VMS_ENV_FILE"] = str(TMP_ROOT / "no-existe.env")
os.environ["VMS_ENV_FILE_ONLY"] = "1"  # ni <repo>/.env ni <datos>/.env (CONTRATO §3.1)
os.environ["PYTHON_KEYRING_BACKEND"] = "vms.core.credentials.MemoryKeyring"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")  # Chromium instalado dentro del venv
(TMP_ROOT / "pytest").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(TMP_ROOT / "pytest"))

from vms.core.config_store import ConfigRepository, ConfigStore  # noqa: E402
from vms.core.credentials import CredentialStore, EncryptedFileBackend  # noqa: E402
from vms.core.paths import AppPaths  # noqa: E402
from vms.core.settings import VmsSettings  # noqa: E402


# =========================================================================== la instalación real, intocable
def _registered_install_dirs() -> list[str]:
    """`InstallDir` y `DataDir` que `vmsctl services install` anota en HKLM\\SOFTWARE\\VMSMultimarca (solo se
    leen): una instalación en otra carpeta o unos datos en otro disco también son intocables."""
    import winreg

    found: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VMSMultimarca") as key:
            for name in ("InstallDir", "DataDir"):
                try:
                    value = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    found.append(value.strip())
    except OSError:  # sin producto instalado no existe la clave
        pass
    return found


def _real_install_dirs() -> tuple[str, ...]:
    """Carpetas del producto instalado de verdad, tomadas al cargar el conftest (antes de que ninguna prueba
    cambie PROGRAMDATA): las de siempre y las del registro. En Linux y macOS no hay ninguna."""
    if sys.platform != "win32":
        return ()
    bases = {os.environ.get("ProgramData"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles")}
    dirs = {os.path.join(b, "VMSMultimarca") for b in bases if b} | set(_registered_install_dirs())
    roots = {os.path.normcase(os.path.abspath(d)) for d in dirs}
    # Nunca una unidad entera ni una carpeta que contenga el repositorio (un DataDir raro en el registro): el
    # vigilante pararía todas las pruebas.
    repo = os.path.normcase(str(ROOT)) + os.sep
    return tuple(sorted(r for r in roots if os.path.dirname(r) != r and not repo.startswith(r + os.sep)))


class RealInstallTouched(PermissionError):
    """Una prueba ha intentado leer o cambiar la instalación real."""


class RealInstallGuard:
    """Gancho de auditoría (PEP 578): ninguna prueba abre, lista, crea, borra ni ejecuta nada dentro de la
    instalación real. Responde como Windows a un usuario normal (PermissionError) y además apunta el intento,
    para que la prueba falle aunque el código se trague el error. Solo vigila mientras corre una prueba.

    Vigila el propio proceso de pytest. No ve lo que haga por dentro un proceso hijo (otro Python, vmsctl…), ni
    el registro (winreg) ni las llamadas directas a Windows con ctypes: para eso está no ser administrador.
    """

    EVENTS = frozenset({"open", "os.listdir", "os.scandir", "os.mkdir", "os.rename", "os.remove", "os.rmdir",
                        "os.chmod", "os.truncate", "os.utime", "os.link", "os.symlink", "shutil.copyfile",
                        "shutil.copytree", "shutil.move", "shutil.rmtree"})
    # Lanzar programas: (programa, argumentos, carpeta, entorno) en subprocess.Popen; parecido en los demás.
    LAUNCH_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2", "os.spawn",
                               "os.exec", "os.posix_spawn"})
    # Lo que puede ir justo detrás de la carpeta en una línea de órdenes («VMSMultimarca-pruebas» no es ella)
    _AFTER_ROOT = r"(?=$|[\\/\"'\s;])"

    def __init__(self, roots: Iterable[str]) -> None:
        self.roots = tuple(roots)
        self.active = False
        self.hits: list[str] = []

    def inside(self, value: object) -> str | None:
        """La ruta (normalizada) si está dentro de alguna carpeta vigilada."""
        if isinstance(value, int) or value is None:
            return None
        try:
            raw = os.fsdecode(os.fspath(value))  # type: ignore[arg-type]
            path = os.path.normcase(os.path.abspath(raw))
        except (TypeError, ValueError, OSError):
            return None
        for root in self.roots:
            if path == root or path.startswith(root + os.sep):
                return raw
        return None

    def mentioned(self, value: object) -> str | None:
        """El texto si nombra alguna carpeta vigilada en cualquier punto: una línea de órdenes entera, también
        con %ProgramData% y compañía sin expandir."""
        if not isinstance(value, (str, bytes)):
            return None
        raw = os.fsdecode(value)
        texts = {os.path.normcase(raw), os.path.normcase(os.path.expandvars(raw))}
        for root in self.roots:
            pattern = re.escape(root) + self._AFTER_ROOT
            if any(re.search(pattern, text) for text in texts):
                return raw
        return None

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.active or not self.roots:
            return
        if event in self.LAUNCH_EVENTS:
            # En Windows, subprocess.Popen avisa con la línea de órdenes ya montada (list2cmdline) en un solo
            # texto: con espacios, el programa va entre comillas («"C:\Program Files\VMSMultimarca\...\vmsctl.exe"
            # health») y no es una ruta. Se mira la línea entera, con sus argumentos (powershell -File
            # C:\ProgramData\VMSMultimarca\x.ps1), además del programa y de la carpeta de trabajo. El entorno no.
            values = [v for a in args if not isinstance(a, dict)
                      for v in (a if isinstance(a, (list, tuple)) else [a])]
            checks: tuple[Callable[[object], str | None], ...] = (self.inside, self.mentioned)
        elif event in self.EVENTS:
            values, checks = list(args[:2]), (self.inside,)
        else:
            return
        for value in values:
            for check in checks:
                hit = check(value)
                if hit is not None:
                    self.hits.append(f"{event} {hit[:300]}")
                    raise RealInstallTouched(13, "Las pruebas no pueden tocar la instalación real", hit)


REAL_INSTALL_GUARD = RealInstallGuard(_real_install_dirs())
sys.addaudithook(REAL_INSTALL_GUARD)
WINDOWS_TESTS = ROOT / "tests" / "windows"


def real_install_allowed(path: Path) -> bool:
    """El e2e del instalador comprueba la instalación de verdad (solo en una VM o en GitHub Actions). La misma
    regla que tests/windows/conftest.is_e2e_file: ``e2e/*.py`` (B3) y ``test_update_*.py`` (B4)."""
    p = Path(path).resolve()
    return p.parent == WINDOWS_TESTS / "e2e" or (p.parent == WINDOWS_TESTS and p.name.startswith("test_update_"))


@pytest.fixture(autouse=True)
def _real_install_untouched(request: pytest.FixtureRequest) -> Iterator[None]:
    if real_install_allowed(Path(request.node.path)):
        yield
        return
    REAL_INSTALL_GUARD.hits.clear()
    REAL_INSTALL_GUARD.active = True
    try:
        yield
    finally:
        REAL_INSTALL_GUARD.active = False
    if REAL_INSTALL_GUARD.hits:
        hits = list(REAL_INSTALL_GUARD.hits)
        REAL_INSTALL_GUARD.hits.clear()
        pytest.fail("La prueba ha tocado la instalación real de VMS Multimarca: " + "; ".join(hits[:5]) +
                    ". Qué hacer: usa la fixture app_paths (o fija VMS_DATA_DIR en tmp_path) y no leas el .env del "
                    "sistema.", pytrace=False)


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
    from tools import pgserver_tz

    try:
        # Windows: la rueda de pgserver no trae las zonas horarias (tools/pgserver_tz.py)
        with_tz = pgserver_tz.use_timezones(TMP_ROOT)
    except pgserver_tz.TimezoneDataMissing as exc:
        warnings.warn(pgserver_tz.NO_TZ_MESSAGE.format(why=exc), stacklevel=1)
        with_tz = False
    pgdata = TMP_ROOT / f"pg-{os.getpid()}"
    srv = pgserver.get_server(pgdata, cleanup_mode="delete")
    try:
        if with_tz:
            # Servidor en UTC en cualquier PC, como en la CI (initdb toma la zona del equipo). Sin zonas no hay
            # UTC que pedir: se queda en GMT, que es lo mismo
            pgserver_tz.force_utc(srv.get_uri())
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


# =========================================================================== bucle de eventos (Windows)
# El mismo bucle que en producción (vms/core/aio.py). En Windows, pytest-asyncio crea ProactorEventLoop y
# psycopg asíncrono lo rechaza («Psycopg cannot use the 'ProactorEventLoop'»): el panel central no abría su
# grupo de conexiones (PoolTimeout) y fallaba casi todo central/, analytics/ y db/. Esas pruebas, y las que
# montan el panel central (`central_app`), van con SelectorEventLoop como esos procesos. Las del backend siguen
# con Proactor, como el backend (lo necesita para lanzar MediaMTX), para no esconder un psycopg asíncrono
# dentro del backend. En Linux y macOS no cambia nada.
SELECTOR_LOOP_DIRS = tuple(ROOT / "tests" / d for d in ("central", "analytics", "db"))


def needs_selector_loop(path: Path, fixturenames: Iterable[str]) -> bool:
    p = Path(path).resolve()
    return any(p.is_relative_to(d) for d in SELECTOR_LOOP_DIRS) or "central_app" in fixturenames


if sys.platform == "win32":
    import asyncio

    def pytest_asyncio_loop_factories(config: pytest.Config,
                                      item: pytest.Item) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
        """Gancho de pytest-asyncio (>= 1.4): factoría del bucle de cada prueba asíncrona."""
        if needs_selector_loop(item.path, getattr(item, "fixturenames", ())):
            return {"selector": asyncio.SelectorEventLoop}
        return {"proactor": asyncio.ProactorEventLoop}


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
