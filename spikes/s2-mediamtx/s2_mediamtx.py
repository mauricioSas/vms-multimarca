"""Prueba de concepto S2: MediaMTX v1.21.1 con el YAML como fuente única de las rutas.

    .venv/bin/python spikes/s2-mediamtx/s2_mediamtx.py [--mediamtx bin/mediamtx] [--out resultado.json]

Monta dos MediaMTX en puertos libres de 127.0.0.1:
  - «origen»: hace de cámaras. Exige usuario y contraseña para leer (como un NVR) y recibe tres
    flujos de prueba de ffmpeg (cam1, cam2, cam3).
  - «motor»: el VMSEngine de la v2. Sus rutas viven SOLO en su mediamtx.yml (nadie usa la API para
    añadirlas) y graban en fMP4.

Comprueba (PLAN-V2 §2.5 y §6.1, S2):
  1. Renombrado atómico (temporal + os.replace) del YAML → MediaMTX lo recarga.
  2. Recarga por ruta: al cambiar el origen de cam-b y añadir cam-c, cam-a NO se reinicia (mismo
     `readyTime`, sin segmento nuevo) y cam-b sí.
  3. Cambio en caliente de un ajuste de grabación (`recordDeleteAfter` en `pathDefaults`) sin
     reiniciar ninguna ruta.
  4. Grabación sin backend: se mata el motor (SIGKILL) y se arranca con el mismo YAML → todas las
     rutas vuelven a grabar sin que nadie llame a la API.
  5. El registro de MediaMTX no contiene la contraseña de las cámaras (tampoco con un 401).
Sale con 0 si todo da lo esperado. Herramienta de laboratorio: ffmpeg solo genera los flujos de prueba.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Sim#Pass@1-x"           # el origen es un MediaMTX: no admite «:», «/» ni «%» en sus usuarios internos
USER = "visor"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_ffmpeg() -> str:
    cand = os.environ.get("VMS_TEST_FFMPEG") or shutil.which("ffmpeg")
    if not cand:
        raise SystemExit("Hace falta ffmpeg (PATH o VMS_TEST_FFMPEG) para generar los flujos de prueba")
    return cand


def segment_gaps(folder: Path, ffprobe: str | None) -> list[float]:
    """Huecos (s) entre segmentos consecutivos: inicio del siguiente − (inicio + duración del anterior)."""
    from datetime import datetime
    if not ffprobe or not folder.exists():
        return []
    segs = []
    for f in sorted(folder.glob("*.mp4")):
        start = datetime.strptime(f.stem, "%Y-%m-%d_%H-%M-%S-%f%z").timestamp()
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                             capture_output=True, text=True).stdout.strip()
        try:
            segs.append((start, float(out)))
        except ValueError:
            continue
    return [round(b[0] - (a[0] + a[1]), 3) for a, b in zip(segs, segs[1:], strict=False)]


def atomic_write_yaml(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Mtx:
    def __init__(self, binary: str, conf: Path, log: Path) -> None:
        self.binary, self.conf, self.log = binary, conf, log
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        f = open(self.log, "ab")  # noqa: SIM115 - lo cierra el proceso hijo al terminar
        self.proc = subprocess.Popen([self.binary, str(self.conf)], stdout=f, stderr=subprocess.STDOUT)

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def base_conf(rtsp_port: int, api_port: int | None) -> dict[str, Any]:
    return {
        "logLevel": "info", "logDestinations": ["stdout"],
        "authMethod": "internal",
        "api": api_port is not None, **({"apiAddress": f"127.0.0.1:{api_port}"} if api_port else {}),
        "metrics": False, "pprof": False, "playback": False,
        "rtsp": True, "rtspAddress": f"127.0.0.1:{rtsp_port}", "rtspTransports": ["tcp"],
        "rtmp": False, "hls": False, "srt": False, "moq": False, "webrtc": False,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mediamtx", default=os.environ.get("VMS_MEDIAMTX_BIN") or str(ROOT / "bin" / "mediamtx"))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--keep", action="store_true", help="no borrar la carpeta de trabajo")
    a = ap.parse_args(argv)
    ffmpeg = find_ffmpeg()
    ffprobe = os.environ.get("VMS_TEST_FFPROBE") or shutil.which("ffprobe")
    version = subprocess.run([a.mediamtx, "--version"], capture_output=True, text=True).stdout.strip()
    work = Path(tempfile.mkdtemp(prefix="vms-s2-"))
    res: dict[str, Any] = {"mediamtx": version, "checks": {}}
    procs: list[subprocess.Popen[bytes]] = []
    origin = engine = None

    def check(name: str, ok: bool, detail: Any) -> None:
        res["checks"][name] = {"ok": bool(ok), "detail": detail}

    try:
        # ------------------------------------------------------------------ origen (cámaras)
        o_rtsp = free_port()
        oconf = base_conf(o_rtsp, None)
        oconf["authInternalUsers"] = [
            {"user": "any", "pass": "", "ips": ["127.0.0.1/32"], "permissions": [{"action": "publish"}]},
            {"user": USER, "pass": PASSWORD, "ips": [], "permissions": [{"action": "read"}]},
        ]
        oconf["paths"] = {"all_others": {}}
        (work / "origin.yml").write_text(yaml.safe_dump(oconf, sort_keys=False), encoding="utf-8")
        origin = Mtx(a.mediamtx, work / "origin.yml", work / "origin.log")
        origin.start()
        time.sleep(1.0)
        for i in (1, 2, 3):
            procs.append(subprocess.Popen(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                 "-i", "testsrc2=size=320x240:rate=10", "-c:v", "libx264", "-preset", "ultrafast",
                 "-tune", "zerolatency", "-g", "10", "-pix_fmt", "yuv420p", "-f", "rtsp",
                 "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:{o_rtsp}/cam{i}"],
                stdout=subprocess.DEVNULL, stderr=open(work / f"ffmpeg{i}.log", "wb")))  # noqa: SIM115
        time.sleep(2.0)

        def src(n: int, password: str = PASSWORD) -> str:
            return f"rtsp://{USER}:{quote(password, safe='')}@127.0.0.1:{o_rtsp}/cam{n}"

        # ------------------------------------------------------------------ motor (VMSEngine)
        e_rtsp, e_api = free_port(), free_port()
        rec = work / "recordings"
        econf = base_conf(e_rtsp, e_api)
        econf["authInternalUsers"] = [{"user": "any", "pass": "", "ips": ["127.0.0.1/32"],
                                       "permissions": [{"action": "api"}, {"action": "read"}]}]
        econf["pathDefaults"] = {
            "rtspTransport": "tcp", "record": False, "recordFormat": "fmp4",
            "recordPath": f"{rec.as_posix()}/%path/%Y-%m-%d_%H-%M-%S-%f%z",
            "recordPartDuration": "1s", "recordSegmentDuration": "1h", "recordDeleteAfter": "24h",
        }
        econf["paths"] = {
            "cam-a/main": {"source": src(1), "record": True},
            "cam-b/main": {"source": src(2), "record": True},
        }
        yml = work / "engine" / "mediamtx.yml"
        yml.parent.mkdir()
        atomic_write_yaml(yml, econf)
        engine = Mtx(a.mediamtx, yml, work / "engine.log")
        engine.start()
        api = httpx.Client(base_url=f"http://127.0.0.1:{e_api}", timeout=3)

        def paths() -> dict[str, dict[str, Any]]:
            try:
                items = api.get("/v3/paths/list").json()["items"]
            except (httpx.HTTPError, ValueError, KeyError):
                return {}
            return {p["name"]: p for p in items}

        def wait_ready(names: list[str], timeout: float = 20) -> dict[str, dict[str, Any]]:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                p = paths()
                if all(n in p and p[n].get("ready") for n in names):
                    return p
                time.sleep(0.2)
            raise RuntimeError(f"Rutas sin vídeo tras {timeout} s: {names}; estado: {paths()}")

        def segments(name: str) -> list[str]:
            d = rec / name
            return sorted(x.name for x in d.glob("*.mp4")) if d.exists() else []

        p0 = wait_ready(["cam-a/main", "cam-b/main"])
        time.sleep(3)   # que haya algo grabado
        before = {n: p0[n]["readyTime"] for n in ("cam-a/main", "cam-b/main")}
        seg_before = {n: segments(n) for n in ("cam-a/main", "cam-b/main")}
        check("grabacion_desde_yaml", all(seg_before.values()),
              {"segmentos": seg_before, "nota": "rutas solo en el YAML; nadie llamó a la API para crearlas"})

        # ---- 1+2: renombrado atómico con cambio de cam-b y alta de cam-c
        econf["paths"]["cam-b/main"]["source"] = src(3)
        econf["paths"]["cam-c/main"] = {"source": src(1), "record": True}
        t0 = time.monotonic()
        atomic_write_yaml(yml, econf)
        try:
            p1 = wait_ready(["cam-a/main", "cam-b/main", "cam-c/main"], timeout=15)
            reload_s = round(time.monotonic() - t0, 2)
        except RuntimeError as exc:
            p1, reload_s = paths(), None
            check("recarga_tras_renombrado_atomico", False, str(exc))
        if reload_s is not None:
            check("recarga_tras_renombrado_atomico", True, {"segundos_hasta_cam_c_con_video": reload_s})
            time.sleep(2.5)
            a_same = p1["cam-a/main"]["readyTime"] == before["cam-a/main"]
            b_changed = p1["cam-b/main"]["readyTime"] != before["cam-b/main"]
            seg_a, seg_b = segments("cam-a/main"), segments("cam-b/main")
            check("recarga_por_ruta_no_toca_las_demas",
                  a_same and seg_a == seg_before["cam-a/main"],
                  {"cam-a readyTime igual": a_same, "cam-a segmentos": seg_a})
            check("recarga_por_ruta_reinicia_la_cambiada",
                  b_changed and len(seg_b) > len(seg_before["cam-b/main"]),
                  {"cam-b readyTime cambió": b_changed, "cam-b segmentos": seg_b})

        # ---- 3: ajuste de grabación en caliente (pathDefaults.recordDeleteAfter)
        snap = {n: p["readyTime"] for n, p in paths().items()}
        segs = {n: segments(n) for n in snap}
        econf["pathDefaults"]["recordDeleteAfter"] = "48h"
        atomic_write_yaml(yml, econf)
        time.sleep(4)
        conf_a = api.get("/v3/config/paths/get/cam-a/main").json()
        after = {n: p["readyTime"] for n, p in paths().items()}
        check("ajuste_de_grabacion_en_caliente_sin_reiniciar_la_ruta",
              after == snap and conf_a.get("recordDeleteAfter") in ("48h", "2d"),   # MediaMTX normaliza 48h → 2d
              {"recordDeleteAfter_aplicado": conf_a.get("recordDeleteAfter"), "rutas_reiniciadas":
               sorted(n for n in snap if after.get(n) != snap[n]),
               "segmentos_nuevos": {n: sorted(set(segments(n)) - set(segs[n])) for n in snap}})
        time.sleep(1.5)
        res["hueco_al_cambiar_pathDefaults_s"] = {n: segment_gaps(rec / n, ffprobe) for n in snap}

        # ---- 3 bis: cambio de recordSegmentDuration (¿reinicia?) — solo se informa
        snap = {n: p["readyTime"] for n, p in paths().items()}
        econf["pathDefaults"]["recordSegmentDuration"] = "30m"
        atomic_write_yaml(yml, econf)
        time.sleep(4)
        after = {n: p["readyTime"] for n, p in paths().items()}
        res["informativo_recordSegmentDuration"] = {
            "rutas_reiniciadas": sorted(n for n in snap if after.get(n) != snap[n])}

        # ---- 3 ter: desactivar la grabación de una sola ruta (cam-c) — solo se informa
        snap = {n: p["readyTime"] for n, p in paths().items()}
        econf["paths"]["cam-c/main"]["record"] = False
        atomic_write_yaml(yml, econf)
        time.sleep(4)
        after = {n: p["readyTime"] for n, p in paths().items()}
        res["informativo_record_false_en_una_ruta"] = {
            "rutas_reiniciadas": sorted(n for n in snap if after.get(n) != snap[n])}
        econf["paths"]["cam-c/main"]["record"] = True
        atomic_write_yaml(yml, econf)
        time.sleep(3)

        # ---- 4: motor muerto (SIGKILL) y arrancado con el mismo YAML, sin backend
        segs_before_kill = {n: segments(n) for n in ("cam-a/main", "cam-b/main", "cam-c/main")}
        engine.kill()
        time.sleep(1)
        t0 = time.monotonic()
        engine.start()
        try:
            wait_ready(["cam-a/main", "cam-b/main", "cam-c/main"], timeout=20)
            time.sleep(2.5)
            segs_after = {n: segments(n) for n in segs_before_kill}
            grew = all(len(segs_after[n]) > len(segs_before_kill[n]) for n in segs_after)
            check("vuelve_a_grabar_sin_backend_tras_sigkill", grew,
                  {"segundos_hasta_video": round(time.monotonic() - t0 - 2.5, 2),
                   "segmentos_nuevos": {n: len(segs_after[n]) - len(segs_before_kill[n]) for n in segs_after}})
        except RuntimeError as exc:
            check("vuelve_a_grabar_sin_backend_tras_sigkill", False, str(exc))

        # ---- 5: 401 (contraseña mala en cam-d) y búsqueda de la contraseña en el registro
        econf["paths"]["cam-d/main"] = {"source": src(2, password="Mala#Clave@9-z"), "record": True}
        atomic_write_yaml(yml, econf)
        time.sleep(6)
        engine_log = (work / "engine.log").read_text(encoding="utf-8", errors="replace")
        needles = [PASSWORD, quote(PASSWORD, safe=""), "Mala#Clave@9-z", quote("Mala#Clave@9-z", safe="")]
        leaked = [n for n in needles if n in engine_log]
        auth_lines = [ln for ln in engine_log.splitlines() if "cam-d" in ln][:3]
        check("registro_sin_contrasenas", not leaked,
              {"contraseñas_encontradas": leaked, "lineas_cam_d": auth_lines})
        res["api_expone_source_con_contrasena"] = PASSWORD in json.dumps(
            api.get("/v3/config/paths/get/cam-a/main").json()) or quote(PASSWORD, safe="") in json.dumps(
            api.get("/v3/config/paths/get/cam-a/main").json())
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        if engine:
            engine.stop()
        if origin:
            origin.stop()
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            res["workdir"] = str(work)

    res["all_ok"] = all(c["ok"] for c in res["checks"].values())
    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    return 0 if res["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
