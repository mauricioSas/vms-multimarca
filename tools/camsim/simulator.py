"""Simulador de cámaras y NVR para pruebas sin hardware.

Levanta un MediaMTX interno (solo 127.0.0.1) al que ffmpeg publica un flujo H.264 por canal
y subflujo (testsrc2 con nombre del equipo, canal y reloj, o un vídeo en bucle). Delante,
un RtspRewriteProxy por equipo imita las rutas nativas:

    Hikvision  rtsp://admin:***@127.0.0.1:<puerto>/Streaming/Channels/101   (102 = subflujo)
    Dahua      rtsp://admin:***@127.0.0.1:<puerto>/cam/realmonitor?channel=1&subtype=0
    Genérico   rtsp://admin:***@127.0.0.1:<puerto>/ch1/main

Permite matar y relanzar un flujo concreto o «desenchufar» un equipo entero para probar
la reconexión. Todos los puertos se eligen libres al arrancar (pruebas en paralelo seguras).
Herramienta de desarrollo: usa el ffmpeg del equipo de desarrollo; no se distribuye.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from .rtsp_proxy import (AuthMode, PathMapper, RtspRewriteProxy, dahua_mapper, generic_mapper,
                         hikvision_mapper)

log = logging.getLogger(__name__)

SimVendor = Literal["hikvision", "dahua", "generic"]
StreamKind = Literal["main", "sub"]
DEFAULT_PASSWORD = "Sim#Pass:1@/x"  # caracteres conflictivos a propósito (# : @ /)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_ffmpeg() -> str:
    exe = os.environ.get("VMS_TEST_FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(exe).is_file():
        raise RuntimeError("No se encontró ffmpeg (define VMS_TEST_FFMPEG)")
    return exe


def find_mediamtx_for_tests() -> str:
    candidates = [os.environ.get("VMS_MEDIAMTX_BIN"),
                  str(Path(__file__).resolve().parents[2] / "bin" / ("mediamtx.exe" if sys.platform == "win32" else "mediamtx")),
                  shutil.which("mediamtx")]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise RuntimeError("No se encontró el binario de MediaMTX: ejecuta «python -m tools.fetch_mediamtx» "
                       "o define VMS_MEDIAMTX_BIN")


def _popen_kwargs() -> dict[str, object]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@dataclass
class SimDevice:
    name: str                                   # [a-z0-9]+, p. ej. «hik1»
    vendor: SimVendor
    channels: int = 1
    username: str = "admin"
    password: str = DEFAULT_PASSWORD
    auth: AuthMode = "digest"
    main_size: tuple[int, int] = (640, 360)
    sub_size: tuple[int, int] = (320, 180)
    fps: int = 10
    videos: dict[int, Path] = field(default_factory=dict)   # canal → vídeo en bucle (si no, testsrc2)
    port: int = 0                                           # se asigna al arrancar

    def native_path(self, channel: int, stream: StreamKind) -> str:
        if self.vendor == "hikvision":
            return f"/Streaming/Channels/{channel}{'01' if stream == 'main' else '02'}"
        if self.vendor == "dahua":
            return f"/cam/realmonitor?channel={channel}&subtype={0 if stream == 'main' else 1}"
        return f"/ch{channel}/{stream}"

    def rtsp_url(self, channel: int, stream: StreamKind = "main", with_credentials: bool = True) -> str:
        from urllib.parse import quote
        auth = ""
        if with_credentials and self.username:
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return f"rtsp://{auth}127.0.0.1:{self.port}{self.native_path(channel, stream)}"

    def mapper(self) -> PathMapper:
        return {"hikvision": hikvision_mapper, "dahua": dahua_mapper,
                "generic": generic_mapper}[self.vendor](self.name)


class _Publisher:
    def __init__(self, ffmpeg: str, url: str, size: tuple[int, int], fps: int, label: str,
                 video: Path | None, log_file: Path, drawtext: bool) -> None:
        self.ffmpeg, self.url, self.size, self.fps = ffmpeg, url, size, fps
        self.label, self.video, self.log_file, self.drawtext = label, video, log_file, drawtext
        self.proc: subprocess.Popen[bytes] | None = None

    def command(self) -> list[str]:
        w, h = self.size
        if self.video:
            inp = ["-re", "-stream_loop", "-1", "-i", str(self.video)]
            vf = [f"scale={w}:{h}", f"fps={self.fps}"]
        else:
            inp = ["-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={self.fps}"]
            vf = []
        if self.drawtext:
            fs = max(12, h // 14)
            vf.append(f"drawtext=text='{self.label}':x=8:y=8:fontsize={fs}:fontcolor=white:box=1:boxcolor=black@0.6")
            vf.append(f"drawtext=text='%{{localtime}}':x=8:y={8 + fs + 6}:fontsize={fs}:fontcolor=white:"
                      "box=1:boxcolor=black@0.6")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *inp, "-an"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
                "-pix_fmt", "yuv420p", "-g", str(self.fps * 2), "-bf", "0",
                "-f", "rtsp", "-rtsp_transport", "tcp", self.url]
        return cmd

    def start(self) -> None:
        if self.running:
            return
        fh = open(self.log_file, "ab")
        try:
            self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=fh,
                                         stderr=subprocess.STDOUT, **_popen_kwargs())  # type: ignore[call-overload]
        finally:
            fh.close()

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg del simulador no terminó tras kill: %s", self.label)
        self.proc = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class CameraSimulator:
    """Uso:
        with CameraSimulator([SimDevice("hik1", "hikvision", channels=2)], workdir) as sim:
            url = sim.device("hik1").rtsp_url(1, "main")
            sim.kill_stream("hik1", 1, "main"); sim.start_stream("hik1", 1, "main")
            sim.set_device_online("hik1", False)
    """

    def __init__(self, devices: list[SimDevice], workdir: Path, *, mediamtx_bin: str | None = None,
                 ffmpeg_bin: str | None = None, rtsp_port: int = 0, api_port: int = 0,
                 drawtext: bool | None = None) -> None:
        names = [d.name for d in devices]
        if len(set(names)) != len(names):
            raise ValueError("Los nombres de equipo del simulador deben ser únicos")
        self.devices = {d.name: d for d in devices}
        self.workdir = Path(workdir).resolve()
        self.mediamtx_bin = mediamtx_bin or find_mediamtx_for_tests()
        self.ffmpeg = ffmpeg_bin or find_ffmpeg()
        self.rtsp_port = rtsp_port or free_port()
        self.api_port = api_port or free_port()
        self._drawtext = drawtext
        self._mtx: subprocess.Popen[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._proxies: dict[str, RtspRewriteProxy] = {}
        self._publishers: dict[tuple[str, int, str], _Publisher] = {}

    # ------------------------------------------------------------------ API pública
    def device(self, name: str) -> SimDevice:
        return self.devices[name]

    def internal_path(self, device: str, channel: int, stream: StreamKind) -> str:
        return f"sim/{device}/ch{channel}/{stream}"

    def internal_url(self, device: str, channel: int, stream: StreamKind) -> str:
        """URL sin proxy ni credenciales (solo 127.0.0.1)."""
        return f"rtsp://127.0.0.1:{self.rtsp_port}/{self.internal_path(device, channel, stream)}"

    def start(self, wait_ready: float = 30.0) -> "CameraSimulator":
        self.workdir.mkdir(parents=True, exist_ok=True)
        atexit.register(self.stop)
        self._start_mediamtx()
        self._start_loop()
        for dev in self.devices.values():
            proxy = RtspRewriteProxy("127.0.0.1", dev.port or 0, "127.0.0.1", self.rtsp_port, dev.mapper(),
                                     username=dev.username, password=dev.password, auth=dev.auth,
                                     realm=f"IP Camera({dev.name.upper()})")
            self._run(proxy.start())
            dev.port = proxy.listen_port
            self._proxies[dev.name] = proxy
        drawtext = self._drawtext if self._drawtext is not None else self._ffmpeg_has_drawtext()
        for dev in self.devices.values():
            for ch in range(1, dev.channels + 1):
                for stream in ("main", "sub"):
                    size = dev.main_size if stream == "main" else dev.sub_size
                    pub = _Publisher(self.ffmpeg, self.internal_url(dev.name, ch, stream), size, dev.fps,
                                     f"{dev.name.upper()} CH{ch} {stream.upper()}", dev.videos.get(ch),
                                     self.workdir / f"ffmpeg-{dev.name}-ch{ch}-{stream}.log", drawtext)
                    pub.start()
                    self._publishers[(dev.name, ch, stream)] = pub
        if wait_ready:
            self.wait_ready(timeout=wait_ready)
        return self

    def stop(self) -> None:
        for pub in self._publishers.values():
            pub.kill()
        self._publishers.clear()
        if self._loop is not None:
            for proxy in self._proxies.values():
                try:
                    self._run(proxy.stop())
                except RuntimeError:
                    pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop = None
        self._proxies.clear()
        if self._mtx is not None and self._mtx.poll() is None:
            self._mtx.terminate()
            try:
                self._mtx.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._mtx.kill()
        self._mtx = None

    def __enter__(self) -> "CameraSimulator":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def kill_stream(self, device: str, channel: int, stream: StreamKind) -> None:
        self._publishers[(device, channel, stream)].kill()

    def start_stream(self, device: str, channel: int, stream: StreamKind, wait: float = 15.0) -> None:
        self._publishers[(device, channel, stream)].start()
        if wait:
            self._wait_paths({self.internal_path(device, channel, stream)}, wait)

    def restart_stream(self, device: str, channel: int, stream: StreamKind, wait: float = 15.0) -> None:
        self.kill_stream(device, channel, stream)
        self.start_stream(device, channel, stream, wait)

    def set_device_online(self, device: str, online: bool) -> None:
        """offline: cierra el puerto del equipo y corta sus conexiones (como un NVR apagado)."""
        proxy = self._proxies[device]
        if online and not proxy.running:
            self._run(proxy.start())
        elif not online and proxy.running:
            self._run(proxy.stop())

    def proxy_connections(self, device: str) -> int:
        """Conexiones RTSP aceptadas en total por el equipo (para verificar «una por canal»)."""
        return self._proxies[device].connections_total

    def ready_paths(self) -> set[str]:
        r = httpx.get(f"http://127.0.0.1:{self.api_port}/v3/paths/list", timeout=5)
        r.raise_for_status()
        return {item["name"] for item in r.json().get("items", []) if item.get("ready")}

    def wait_ready(self, timeout: float = 30.0) -> None:
        self._wait_paths({self.internal_path(d, c, s) for (d, c, s) in self._publishers}, timeout)

    # ------------------------------------------------------------------ interno
    def _wait_paths(self, expected: set[str], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        missing = expected
        while time.monotonic() < deadline:
            try:
                missing = expected - self.ready_paths()
            except httpx.HTTPError:
                missing = expected
            if not missing:
                return
            time.sleep(0.25)
        raise TimeoutError(f"Flujos del simulador sin publicar tras {timeout:.0f} s: {sorted(missing)} "
                           f"(revisa los ffmpeg-*.log en {self.workdir})")

    def _ffmpeg_has_drawtext(self) -> bool:
        try:
            out = subprocess.run([self.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return " drawtext " in out

    def _start_mediamtx(self) -> None:
        cfg = self.workdir / "camsim-mediamtx.yml"
        cfg.write_text(
            "logLevel: warn\n"
            "logDestinations: [stdout]\n"
            "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\nmoq: no\nplayback: no\nmetrics: no\npprof: no\n"
            "api: yes\n"
            f"apiAddress: 127.0.0.1:{self.api_port}\n"
            "rtsp: yes\n"
            "rtspTransports: [tcp]\n"
            f"rtspAddress: 127.0.0.1:{self.rtsp_port}\n"
            "authInternalUsers:\n"
            "  - user: any\n"
            "    pass:\n"
            "    ips: ['127.0.0.1', '::1']\n"
            "    permissions:\n"
            "      - action: publish\n"
            "      - action: read\n"
            "      - action: api\n"
            "paths:\n"
            "  all_others:\n",
            encoding="utf-8")
        log_fh = open(self.workdir / "camsim-mediamtx.log", "ab")
        try:
            self._mtx = subprocess.Popen([self.mediamtx_bin, str(cfg)], cwd=self.workdir, stdin=subprocess.DEVNULL,
                                         stdout=log_fh, stderr=subprocess.STDOUT, **_popen_kwargs())  # type: ignore[call-overload]
        finally:
            log_fh.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._mtx.poll() is not None:
                raise RuntimeError(f"MediaMTX del simulador terminó al arrancar (código {self._mtx.returncode}); "
                                   f"mira {self.workdir / 'camsim-mediamtx.log'}")
            try:
                httpx.get(f"http://127.0.0.1:{self.api_port}/v3/paths/list", timeout=1).raise_for_status()
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise TimeoutError("La API del MediaMTX del simulador no respondió en 15 s")

    def _start_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop

        def runner() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=runner, name="camsim-proxies", daemon=True)
        self._thread.start()

    def _run(self, coro: object) -> object:
        if self._loop is None:
            raise RuntimeError("El simulador no está arrancado")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return fut.result(timeout=10)


def install_sigterm_handler(sim: CameraSimulator) -> None:
    def handler(signum: int, frame: object) -> None:
        sim.stop()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, handler)
