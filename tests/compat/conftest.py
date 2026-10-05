"""Arnés de la matriz de simuladores (PLAN-V2 §4.2): MediaMTX de laboratorio + ffmpeg de pruebas.

Herramientas de laboratorio, nunca en el producto: MediaMTX (MIT) como «cámara» y ffmpeg (el del equipo de
desarrollo) para publicar flujos H.264/H.265/MJPEG con o sin audio.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

# MediaMTX no admite «:» ni «/» en las contraseñas de sus usuarios: el laboratorio usa otra con «#», «@» y «!»
# (los caracteres conflictivos de la contraseña de prueba general se prueban con el servidor caótico y camsim).
LAB_PASSWORD = "Lab#Pass!2@x"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class LabMtx:
    """MediaMTX de laboratorio con usuario `admin`/contraseña de prueba y el método de autenticación pedido."""

    binary: str
    workdir: Path
    auth: str = "digest"
    paths: dict[str, dict[str, object]] = field(default_factory=dict)
    rtsp_port: int = 0
    api_port: int = 0
    proc: subprocess.Popen[bytes] | None = None

    def start(self) -> LabMtx:
        self.rtsp_port, self.api_port = free_port(), free_port()
        self.workdir.mkdir(parents=True, exist_ok=True)
        lines = ["logLevel: warn", "hls: no", "webrtc: no", "rtmp: no", "srt: no", "moq: no", "playback: no",
                 "metrics: no", "api: yes", f"apiAddress: 127.0.0.1:{self.api_port}", "rtsp: yes",
                 "rtspTransports: [tcp]", f"rtspAddress: 127.0.0.1:{self.rtsp_port}",
                 f"rtspAuthMethods: [{self.auth}]", "authInternalUsers:", "  - user: admin",
                 f"    pass: '{LAB_PASSWORD}'", "    ips: []", "    permissions:", "      - action: publish",
                 "      - action: read", "      - action: api", "paths:"]
        for name, conf in self.paths.items():
            lines.append(f"  {name}:")
            lines += [f"    {k}: {v}" for k, v in conf.items()]
        lines += ["  all_others:"]
        cfg = self.workdir / f"lab-mtx-{self.auth}.yml"
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log = open(self.workdir / f"lab-mtx-{self.auth}.log", "ab")
        try:
            self.proc = subprocess.Popen([self.binary, str(cfg)], stdout=log, stderr=subprocess.STDOUT,
                                         **({"start_new_session": True} if sys.platform != "win32" else {}))  # type: ignore[call-overload]
        finally:
            log.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{self.api_port}/v3/paths/list", auth=("admin", LAB_PASSWORD),
                             timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise TimeoutError("MediaMTX de laboratorio no arrancó")

    def url(self, path: str, *, credentials: bool = True) -> str:
        auth = f"admin:{quote(LAB_PASSWORD, safe='')}@" if credentials else ""
        return f"rtsp://{auth}127.0.0.1:{self.rtsp_port}/{path.lstrip('/')}"

    def ready(self) -> set[str]:
        r = httpx.get(f"http://127.0.0.1:{self.api_port}/v3/paths/list", auth=("admin", LAB_PASSWORD), timeout=3)
        return {i["name"] for i in r.json().get("items", []) if i.get("ready")}

    def wait_ready(self, names: set[str], timeout: float = 25.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if names <= self.ready():
                return
            time.sleep(0.2)
        raise TimeoutError(f"rutas sin vídeo: {sorted(names - self.ready())} (mira {self.workdir})")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@dataclass
class Publisher:
    """ffmpeg publicando un flujo sintético (testsrc2) en una ruta del MediaMTX de laboratorio."""

    ffmpeg: str
    url: str
    video: list[str]
    audio: list[str] = field(default_factory=list)
    log: Path | None = None
    proc: subprocess.Popen[bytes] | None = None

    def start(self) -> Publisher:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
               "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15"]
        if self.audio:
            cmd += ["-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=8000"]
        cmd += self.video + (self.audio or ["-an"]) + ["-f", "rtsp", "-rtsp_transport", "tcp", self.url]
        fh = open(self.log, "ab") if self.log else subprocess.DEVNULL
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,  # type: ignore[arg-type]
                                     **({"start_new_session": True} if sys.platform != "win32" else {}))  # type: ignore[call-overload]
        return self

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            if sys.platform != "win32":
                os.killpg(self.proc.pid, signal.SIGKILL)
            else:
                self.proc.kill()
            self.proc.wait(timeout=5)


H264_BASE = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
             "-pix_fmt", "yuv420p", "-g", "15", "-bf", "0"]
H264_HIGH = ["-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "15",
             "-bf", "2"]
H265 = ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-x265-params",
        "log-level=error:keyint=15:min-keyint=15:bframes=0"]
MJPEG = ["-c:v", "mjpeg", "-huffman", "default", "-q:v", "6", "-pix_fmt", "yuvj420p"]  # RFC 2435: tablas estándar
GOP10 = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
         "-pix_fmt", "yuv420p", "-g", "150", "-keyint_min", "150", "-sc_threshold", "0", "-bf", "0"]
PCMA = ["-c:a", "pcm_alaw", "-ar", "8000", "-ac", "1"]
AAC = ["-c:a", "aac", "-b:a", "32k"]


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory, mediamtx_bin: str, ffmpeg_bin: str) -> Iterator[LabMtx]:
    """MediaMTX (Digest) con un flujo por caso de la matriz, publicado por ffmpeg."""
    work = tmp_path_factory.mktemp("compat")
    mtx = LabMtx(mediamtx_bin, work).start()
    streams = {
        "Streaming/Channels/101": (H264_BASE, []),          # Hikvision: principal H.264
        "Streaming/Channels/102": (H265, []),               # Hikvision: subflujo H.265 (caso «Corregir códec»)
        "unicast/c1/s0/live": (H265, AAC),                  # Uniview: principal H.265 con AAC
        "unicast/c1/s1/live": (H264_BASE, PCMA),            # Uniview: subflujo H.264 con PCMA
        "stream1": (H264_HIGH, []),                         # VIGI: H.264 high con B-frames
        "Preview_01_main": (MJPEG, []),                     # Reolink en MJPEG (caso MJPEG por RTSP)
        "gop10": (GOP10, []),                               # GOP de 10 s (H.264+/H.265+)
    }
    pubs = [Publisher(ffmpeg_bin, mtx.url(name), video, audio, work / f"ffmpeg-{i}.log").start()
            for i, (name, (video, audio)) in enumerate(streams.items())]
    try:
        mtx.wait_ready(set(streams), timeout=40)
        yield mtx
    finally:
        for p in pubs:
            p.stop()
        mtx.stop()
