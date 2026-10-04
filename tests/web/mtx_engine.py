"""Motor MediaMTX mínimo para las pruebas de la interfaz web (cumple el Protocol `Engine`).

NO es el motor del producto (ese lo implementa vms.engine). Sirve para que las pruebas de
la interfaz tengan vídeo real: WebRTC (WHEP), grabación fMP4 y reproducción (/list y /get),
sin depender de que el motor definitivo esté terminado. Todo escucha en 127.0.0.1.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx

from vms.core.errors import EngineUnavailable
from vms.core.interfaces import CameraSource, DiskUsage, EngineStatus, PathStatus, RecordingSpan
from vms.core.models import RecordingSettings, RetentionSettings
from vms.core.naming import mtx_path, parse_mtx_path

log = logging.getLogger("tests.web.mtx_engine")


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _free_udp_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class MtxTestEngine:
    """MediaMTX real con WebRTC + grabación + playback, rutas añadidas por su API."""

    def __init__(self, mediamtx_bin: str, workdir: Path, *, segment_seconds: int = 60) -> None:
        self.exe = mediamtx_bin
        self.workdir = Path(workdir)
        self.recordings_dir = self.workdir / "recordings"
        self.segment_seconds = segment_seconds
        self.rtsp_port = _free_port()
        self.api_port = _free_port()
        self.webrtc_port = _free_port()
        self.ice_port = _free_udp_port()
        self.playback_port = _free_port()
        self.proc: subprocess.Popen[bytes] | None = None
        self.started_at: datetime | None = None
        self._applied: dict[str, dict[str, Any]] = {}
        self._api = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.api_port}", timeout=5)
        self._play = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.playback_port}", timeout=10)

    # ------------------------------------------------------------------ ciclo de vida
    def _yaml(self) -> str:
        rec = self.recordings_dir.as_posix()
        return (
            "logLevel: info\nlogDestinations: [stdout]\n"
            "rtmp: no\nhls: no\nsrt: no\nmoq: no\npprof: no\nmetrics: no\n"
            f"api: yes\napiAddress: 127.0.0.1:{self.api_port}\n"
            f"rtsp: yes\nrtspTransports: [tcp]\nrtspAddress: 127.0.0.1:{self.rtsp_port}\n"
            f"webrtc: yes\nwebrtcAddress: 127.0.0.1:{self.webrtc_port}\n"
            f"webrtcLocalUDPAddress: 127.0.0.1:{self.ice_port}\nwebrtcLocalTCPAddress: ''\n"
            "webrtcIPsFromInterfaces: no\nwebrtcAdditionalHosts: [127.0.0.1]\n"
            f"playback: yes\nplaybackAddress: 127.0.0.1:{self.playback_port}\n"
            "authInternalUsers:\n  - user: any\n    pass:\n    ips: ['127.0.0.1', '::1']\n"
            "    permissions:\n      - action: read\n      - action: playback\n      - action: api\n"
            "pathDefaults:\n  rtspTransport: tcp\n  recordFormat: fmp4\n"
            f"  recordPath: {rec}/%path/%Y-%m-%d_%H-%M-%S-%f\n"
            f"  recordPartDuration: 1s\n  recordSegmentDuration: {self.segment_seconds}s\n"
            "  recordDeleteAfter: 24h\n"
            "paths: {}\n")

    def start_sync(self, timeout: float = 15.0) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.workdir / "mediamtx.yml"
        cfg.write_text(self._yaml(), encoding="utf-8")
        kwargs: dict[str, Any] = ({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32"
                                  else {"start_new_session": True})
        with open(self.workdir / "mediamtx.log", "ab") as fh:
            self.proc = subprocess.Popen([self.exe, str(cfg)], cwd=self.workdir, stdin=subprocess.DEVNULL,
                                         stdout=fh, stderr=subprocess.STDOUT, **kwargs)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"MediaMTX terminó al arrancar; mira {self.workdir / 'mediamtx.log'}")
            try:
                httpx.get(f"http://127.0.0.1:{self.api_port}/v3/paths/list", timeout=1).raise_for_status()
                self.started_at = datetime.now(timezone.utc)
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise TimeoutError("La API de MediaMTX no respondió")

    def stop_sync(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    async def start(self) -> None:
        if self.proc is None:
            self.start_sync()

    async def stop(self) -> None:
        self.stop_sync()
        await self._api.aclose()
        await self._play.aclose()

    # ------------------------------------------------------------------ rutas
    async def apply(self, sources: list[CameraSource], recording: RecordingSettings,
                    retention: RetentionSettings, recordings_dir: str) -> None:
        if self.proc is None or self.proc.poll() is not None:
            raise EngineUnavailable("El motor de vídeo no está en marcha")
        wanted: dict[str, dict[str, Any]] = {}
        for s in sources:
            wanted[mtx_path(s.camera_id, "main")] = {
                "source": s.main_url, "rtspTransport": s.rtsp_transport, "record": s.record}
            if s.sub_url:
                wanted[mtx_path(s.camera_id, "sub")] = {
                    "source": s.sub_url, "rtspTransport": s.rtsp_transport, "record": False,
                    "sourceOnDemand": True, "sourceOnDemandCloseAfter": "30s"}
        try:
            for name in list(self._applied):
                if name not in wanted:
                    r = await self._api.delete(f"/v3/config/paths/delete/{quote(name, safe='/')}")
                    if r.status_code not in (200, 404):
                        log.warning("No se pudo borrar la ruta %s: %s", name, r.status_code)
                    self._applied.pop(name, None)
            for name, conf in wanted.items():
                if self._applied.get(name) == conf:
                    continue
                verb = "replace" if name in self._applied else "add"
                r = await self._api.post(f"/v3/config/paths/{verb}/{quote(name, safe='/')}", json=conf)
                if r.status_code != 200:
                    log.error("MediaMTX rechazó la ruta %s (%s): %s", name, verb, r.text[:200])
                    continue
                self._applied[name] = conf
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"MediaMTX no responde: {exc}") from exc

    async def status(self) -> EngineStatus:
        running = self.proc is not None and self.proc.poll() is None
        api_ok = False
        if running:
            try:
                api_ok = (await self._api.get("/v3/paths/list")).status_code == 200
            except httpx.HTTPError:
                api_ok = False
        return EngineStatus(running=running, pid=self.proc.pid if running and self.proc else None,
                            version="v1.21.1", started_at=self.started_at, api_ok=api_ok)

    async def paths_status(self) -> dict[str, PathStatus]:
        try:
            r = await self._api.get("/v3/paths/list", params={"itemsPerPage": 1000})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"MediaMTX no responde: {exc}") from exc
        out: dict[str, PathStatus] = {}
        for item in r.json().get("items", []):
            name = item.get("name", "")
            parsed = parse_mtx_path(name)
            conf = self._applied.get(name, {})
            ready = bool(item.get("ready"))
            out[name] = PathStatus(
                name=name, camera_id=parsed[0] if parsed else None,
                stream=parsed[1] if parsed else None,  # type: ignore[arg-type]
                ready=ready, source_online=ready, readers=len(item.get("readers") or []),
                bytes_received=int(item.get("bytesReceived") or 0),
                tracks=list(item.get("tracks") or []),
                recording=bool(conf.get("record")) and ready)
        return out

    # ------------------------------------------------------------------ grabaciones
    async def list_recordings(self, camera_id: str, start: datetime | None,
                              end: datetime | None) -> list[RecordingSpan]:
        params: dict[str, str] = {"path": mtx_path(camera_id, "main")}
        if start:
            params["start"] = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if end:
            params["end"] = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            r = await self._play.get("/list", params=params)
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"El servidor de reproducción no responde: {exc}") from exc
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise EngineUnavailable(f"Reproducción: respuesta {r.status_code}")
        spans = []
        for item in r.json():
            st = datetime.fromisoformat(item["start"]).astimezone(timezone.utc)
            spans.append(RecordingSpan(start=st, duration=float(item["duration"])))
        return spans

    def playback_get_url(self, camera_id: str, start: datetime, duration: float,
                         fmt: Literal["fmp4", "mp4"] = "fmp4") -> str:
        iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return (f"http://127.0.0.1:{self.playback_port}/get?path={quote(mtx_path(camera_id, 'main'), safe='')}"
                f"&start={quote(iso, safe='')}&duration={duration:.3f}&format={fmt}")

    def whep_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        return f"http://127.0.0.1:{self.webrtc_port}/{mtx_path(camera_id, stream)}/whep"

    def rtsp_read_url(self, camera_id: str, stream: Literal["main", "sub"]) -> str:
        return f"rtsp://127.0.0.1:{self.rtsp_port}/{mtx_path(camera_id, stream)}"

    async def disk_usage(self) -> DiskUsage:
        u = shutil.disk_usage(self.recordings_dir)
        return DiskUsage(path=str(self.recordings_dir), total=u.total, used=u.used, free=u.free,
                         percent=round(u.used * 100.0 / u.total, 1) if u.total else 0.0)
