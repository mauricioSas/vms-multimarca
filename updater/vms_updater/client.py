"""Cliente TUF (python-tuf 7 `ngclient`) con las tres fuentes de CONTRATO §15.1.

- `https://updates.<dominio>/<cliente>/` → Worker de Cloudflare: `metadata/` libre y `targets/` con
  `Authorization: Bearer <token de sede>`.
- `http://<host>:<puerto>/` → repositorio estático (pruebas y CI; mismo formato).
- `file:///D:/vms-updates` → espejo USB o carpeta compartida (repositorio `offline`, 60 días).

La verificación TUF es idéntica en las tres: la seguridad no depende del transporte. Nunca se ignora la
caducidad de los metadatos (`ExpiredMetadataError` → `metadata_expired`).
"""
from __future__ import annotations

import email.utils
import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import urllib3
from tuf.api import exceptions as tuf_exc
from tuf.api.metadata import Metadata, TargetFile, Targets, Timestamp
from tuf.ngclient import FetcherInterface, Updater, UpdaterConfig

from . import __version__

log = logging.getLogger("vms_updater.client")

USER_AGENT = f"VMSUpdater/{__version__}"
MAX_JSON_TARGET = 4 * 1024 * 1024   # descriptor, canal o tabla de avisos: nunca más de 4 MB


class UpdateSourceError(Exception):
    """Error al hablar con la fuente (red, servidor, USB). Se reintenta en el siguiente ciclo."""

    code = "source_unavailable"

    def __init__(self, message_es: str) -> None:
        super().__init__(message_es)
        self.message_es = message_es


class MetadataExpired(UpdateSourceError):
    code = "metadata_expired"


class SecurityError(UpdateSourceError):
    """Firma inválida, versión anterior (rollback), hash o longitud que no cuadran: NUNCA se instala."""

    code = "security_error"


class NotAuthorized(UpdateSourceError):
    code = "not_authorized"


@dataclass(frozen=True)
class Source:
    kind: str            # "worker" | "http" | "file"
    base_url: str        # siempre acaba en "/"
    mode: str            # "online" | "offline" (qué root de confianza usa)
    token: str | None = None

    @property
    def metadata_url(self) -> str:
        return self.base_url + "metadata/"

    @property
    def targets_url(self) -> str:
        return self.base_url + "targets/"

    def describe(self) -> str:
        """Para los registros: nunca el token."""
        return f"{self.kind}:{self.base_url}"


def parse_source(url: str, *, token: str | None = None, mode: str | None = None) -> Source:
    url = (url or "").strip()
    if not url:
        raise ValueError("No hay fuente de actualizaciones (VMS_UPDATE_SOURCE vacía)")
    if not url.endswith("/"):
        url += "/"
    scheme = urlparse(url).scheme.lower()
    if scheme == "https":
        return Source("worker", url, mode or "online", token or None)
    if scheme == "http":
        return Source("http", url, mode or "online", token or None)
    if scheme == "file":
        return Source("file", url, mode or "offline", None)
    raise ValueError(f"Fuente de actualizaciones no admitida: {scheme or url!r} (https://, http:// o file:///)")


def file_url_to_path(url: str) -> Path:
    p = urlparse(url)
    raw = url2pathname(p.path)
    if p.netloc and p.netloc not in ("", "localhost"):
        raw = f"//{p.netloc}{raw}"     # recurso compartido UNC: file://servidor/carpeta
    return Path(raw)


# --------------------------------------------------------------------------- transportes
class HttpFetcher(FetcherInterface):
    """urllib3 con el token de la sede solo en `targets/` y registro de la cabecera `Date` (reloj)."""

    def __init__(self, source: Source, *, timeout: float = 30.0, chunk_size: int = 256 * 1024) -> None:
        self.source = source
        self.timeout = timeout
        self.chunk_size = chunk_size
        self.last_server_date: datetime | None = None
        try:  # misma gestión de proxies del sistema que el fetcher por defecto de python-tuf
            from tuf.ngclient._internal.proxy import ProxyEnvironment
            self._pool: Any = ProxyEnvironment(headers={"User-Agent": USER_AGENT})
        except ImportError:  # pragma: no cover - otra versión de python-tuf
            self._pool = urllib3.PoolManager(headers={"User-Agent": USER_AGENT})

    def _headers(self, url: str) -> dict[str, str]:
        h = {"User-Agent": USER_AGENT}
        if self.source.token and url.startswith(self.source.targets_url):
            h["Authorization"] = f"Bearer {self.source.token}"
        return h

    def _fetch(self, url: str) -> Iterator[bytes]:
        try:
            resp = self._pool.request("GET", url, headers=self._headers(url), preload_content=False,
                                      timeout=urllib3.Timeout(self.timeout), retries=urllib3.Retry(2, redirect=0))
        except urllib3.exceptions.MaxRetryError as exc:
            if isinstance(exc.reason, urllib3.exceptions.TimeoutError):
                raise tuf_exc.SlowRetrievalError from exc
            raise tuf_exc.DownloadError(f"sin conexión con {self.source.describe()}") from exc
        except urllib3.exceptions.HTTPError as exc:
            raise tuf_exc.DownloadError(f"error de red con {self.source.describe()}") from exc
        date = resp.headers.get("Date")
        if date:
            try:
                self.last_server_date = email.utils.parsedate_to_datetime(date).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
        if resp.status >= 400:
            resp.close()
            raise tuf_exc.DownloadHTTPError(f"HTTP {resp.status}", resp.status)
        return self._chunks(resp)

    def _chunks(self, resp: Any) -> Iterator[bytes]:
        try:
            yield from resp.stream(self.chunk_size)
        except urllib3.exceptions.TimeoutError as exc:
            raise tuf_exc.SlowRetrievalError from exc
        except urllib3.exceptions.HTTPError as exc:      # conexión cortada a mitad
            raise tuf_exc.DownloadError(f"descarga cortada desde {self.source.describe()}") from exc
        finally:
            resp.release_conn()


class FileFetcher(FetcherInterface):
    """Lee el repositorio de un USB o carpeta compartida. Un archivo que falta es un 404 (así `ngclient`
    sabe que no hay un `root` más nuevo)."""

    last_server_date: datetime | None = None

    def __init__(self, chunk_size: int = 256 * 1024) -> None:
        self.chunk_size = chunk_size

    def _fetch(self, url: str) -> Iterator[bytes]:
        path = file_url_to_path(url)
        try:
            f = open(path, "rb")
        except FileNotFoundError as exc:
            raise tuf_exc.DownloadHTTPError(f"{path.name} no está en el espejo", 404) from exc
        except OSError as exc:
            raise tuf_exc.DownloadError(f"no se pudo leer el espejo ({exc.strerror})") from exc
        return self._chunks(f)

    def _chunks(self, f: Any) -> Iterator[bytes]:
        with f:
            while True:
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break
                yield chunk


def make_fetcher(source: Source) -> HttpFetcher | FileFetcher:
    return FileFetcher() if source.kind == "file" else HttpFetcher(source)


# --------------------------------------------------------------------------- cliente
@dataclass
class RefreshInfo:
    timestamp_expires: datetime
    server_date: datetime | None
    targets: dict[str, TargetFile] = field(default_factory=dict)


class TufClient:
    def __init__(self, source: Source, *, metadata_dir: Path, targets_dir: Path, trusted_root: bytes,
                 fetcher: FetcherInterface | None = None) -> None:
        self.source = source
        self.metadata_dir = Path(metadata_dir) / source.mode
        self.targets_dir = Path(targets_dir)
        self.trusted_root = trusted_root
        self.fetcher = fetcher or make_fetcher(source)
        self._updater: Updater | None = None
        self.info: RefreshInfo | None = None

    def _new_updater(self) -> Updater:
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.targets_dir.mkdir(parents=True, exist_ok=True)
        cached = self.metadata_dir / "root.json"
        cfg = UpdaterConfig(app_user_agent=USER_AGENT)
        kwargs: dict[str, Any] = dict(metadata_dir=str(self.metadata_dir), metadata_base_url=self.source.metadata_url,
                                      target_dir=str(self.targets_dir), target_base_url=self.source.targets_url,
                                      fetcher=self.fetcher, config=cfg)
        if cached.is_file():
            try:
                return Updater(bootstrap=None, **kwargs)
            except (tuf_exc.RepositoryError, ValueError, OSError) as exc:
                log.warning("Caché de metadatos TUF no válida (%s): se empieza desde el root de confianza", exc)
                shutil.rmtree(self.metadata_dir, ignore_errors=True)
                self.metadata_dir.mkdir(parents=True, exist_ok=True)
        return Updater(bootstrap=self.trusted_root, **kwargs)

    def refresh(self) -> RefreshInfo:
        """root → timestamp → snapshot → targets (orden garantizado por python-tuf)."""
        try:
            self._updater = self._new_updater()
            self._updater.refresh()
        except tuf_exc.ExpiredMetadataError as exc:
            raise MetadataExpired(_expired_message(self.source)) from exc
        except tuf_exc.DownloadHTTPError as exc:
            if exc.status_code in (401, 403):
                raise NotAuthorized("El servidor de actualizaciones rechazó el token de la sede "
                                    f"(HTTP {exc.status_code})") from exc
            raise UpdateSourceError(f"El servidor de actualizaciones respondió HTTP {exc.status_code}") from exc
        except tuf_exc.DownloadError as exc:
            raise UpdateSourceError(f"No se pudo descargar la información de versiones: {exc}") from exc
        except tuf_exc.RepositoryError as exc:
            raise SecurityError(f"Los metadatos de actualización no son de confianza ({type(exc).__name__}): "
                                "no se instala nada") from exc
        except OSError as exc:
            raise UpdateSourceError(f"Error de disco o de red: {exc}") from exc
        ts: Metadata[Timestamp] = Metadata.from_file(str(self.metadata_dir / "timestamp.json"))
        tg: Metadata[Targets] = Metadata.from_file(str(self.metadata_dir / "targets.json"))
        server_date = getattr(self.fetcher, "last_server_date", None)
        self.info = RefreshInfo(timestamp_expires=ts.signed.expires, server_date=server_date,
                                targets=dict(tg.signed.targets))
        return self.info

    def targets(self) -> dict[str, TargetFile]:
        if self.info is None:
            raise RuntimeError("refresh() antes de consultar los targets")
        return self.info.targets

    def _info(self, path: str) -> TargetFile:
        if self._updater is None:
            raise RuntimeError("refresh() antes de descargar")
        info = self._updater.get_targetinfo(path)
        if info is None:
            raise SecurityError(f"«{path}» no está en targets.json firmado: no se descarga")
        return info

    def download(self, path: str) -> Path:
        """Descarga (o reutiliza de la caché) y verifica longitud y SHA-256 firmados."""
        info = self._info(path)
        assert self._updater is not None
        dest = self.targets_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        cached = self._updater.find_cached_target(info, str(dest))
        if cached:
            return Path(cached)
        try:
            return Path(self._updater.download_target(info, str(dest)))
        except tuf_exc.LengthOrHashMismatchError as exc:
            raise SecurityError(f"«{path}» no coincide con el hash firmado: descartado") from exc
        except tuf_exc.DownloadHTTPError as exc:
            if exc.status_code in (401, 403):
                raise NotAuthorized(f"Descarga de «{path}» rechazada (HTTP {exc.status_code}): "
                                    "revisa el token de la sede") from exc
            raise UpdateSourceError(f"Descarga de «{path}» fallida (HTTP {exc.status_code})") from exc
        except tuf_exc.DownloadError as exc:
            raise UpdateSourceError(f"Descarga de «{path}» fallida: {exc}") from exc

    def read(self, path: str, *, max_length: int = MAX_JSON_TARGET) -> bytes:
        info = self._info(path)
        if info.length > max_length:
            raise SecurityError(f"«{path}» es demasiado grande ({info.length} bytes)")
        return self.download(path).read_bytes()

    def purge_cache(self, keep: set[str]) -> None:
        """Borra de la caché los targets que ya no hacen falta."""
        if not self.targets_dir.is_dir():
            return
        keep_paths = {(self.targets_dir / k).resolve() for k in keep}
        for p in self.targets_dir.rglob("*"):
            if p.is_file() and p.resolve() not in keep_paths:
                try:
                    p.unlink()
                except OSError:
                    pass


def _expired_message(source: Source) -> str:
    if source.kind == "file":
        return ("Los metadatos del espejo USB han caducado (cada USB sirve 60 días desde que se prepara): "
                "prepara un USB nuevo con «python -m tools.release mirror». No se aplica nada.")
    return ("Los metadatos de actualización han caducado (servidor sin refrescar o reloj del equipo "
            "desfasado): no se aplica nada.")
