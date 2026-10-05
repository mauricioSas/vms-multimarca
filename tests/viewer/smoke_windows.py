"""Prueba de humo del visor en Windows por CDP (PLAN-V2 §4.6 paso 5 y §6.2 B2, criterios 3 a 6).

    python -m tests.viewer.smoke_windows --exe native/viewer/src-tauri/target/release/VMS.exe --out visor-smoke

Usa la build de PRUEBA del visor (`--features prueba`: abre el puerto CDP de WebView2 con VMS_VIEWER_CDP_PORT) y
se conecta a ella por CDP directo (`/json/list` + `websockets`, que ya viene en el lock de sede). Monta lo mismo que una tienda en
pequeño: simulador de cámaras (MediaMTX + ffmpeg de pruebas) + backend real (`python -m vms`), y comprueba:

1. muro: `VMS.exe --walls` abre /wall/1 entrando con el token de kiosco del archivo (sin login ni token en la
   URL) y cada celda pinta ≥ 30 fotogramas en 10 s (`requestVideoFrameCallback`); el panel va en otro perfil de
   WebView2 (otra carpeta de datos, otro navegador): iniciar sesión en él no toca la sesión de kiosco del muro;
2. capacidades: desde la página del backend, `invoke()` de cualquier comando del visor se rechaza; desde una
   página local (segunda instancia con `--abrir diagnostico`) funciona;
3. sin permiso: con una ACL que niega la lectura de `kiosk.token` al usuario, el muro muestra «Sin permiso para
   abrir los muros» (y el usuario no puede leer el archivo);
4. S5 (opción A): servidor HTTPS autofirmado con la huella fijada → carga (también tras una redirección 302 y tras
   reiniciar el servidor); con otro certificado → aviso «El certificado del servidor cambió» y no carga nada.

Escribe `<out>/results.json` (mismo espíritu que tests/e2e/RESULTADOS.md), capturas y registros. Sale con 1 si
falla algo. Herramienta de pruebas: no forma parte del producto.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parents[2]
CDP_PORT = 9222
# El visor abre dos navegadores WebView2 (dos carpetas de datos): los muros en CDP_PORT y el panel y las páginas
# locales en CDP_PORT + 1 (app.rs::browser_args).
CDP_PORTS = (CDP_PORT, CDP_PORT + 1)
FRAMES_MIN = 30

FRAMES_JS = """async (ms) => {
  const vids = [...document.querySelectorAll('.cell:not(.empty) video')];
  const counts = vids.map(() => 0);
  vids.forEach((v, i) => {
    if (!v.requestVideoFrameCallback) return;
    const cb = () => { counts[i]++; v.requestVideoFrameCallback(cb); };
    v.requestVideoFrameCallback(cb);
  });
  const q0 = vids.map(v => v.getVideoPlaybackQuality().totalVideoFrames);
  await new Promise(r => setTimeout(r, ms));
  const q1 = vids.map(v => v.getVideoPlaybackQuality().totalVideoFrames);
  return {cells: vids.length, rvfc: counts, decoded: q1.map((x, i) => x - q0[i]),
          size: vids.map(v => [v.videoWidth, v.videoHeight])};
}"""

LOGIN_JS = """async ([u, p]) => (await fetch('/api/auth/login', {method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-Requested-With': 'vms'},
  body: JSON.stringify({username: u, password: p})})).status"""

ME_JS = """async (wall) => (await fetch('/api/auth/me', {headers: wall ? {'X-Requested-With': 'vms',
  'X-VMS-Client': 'wall'} : {'X-Requested-With': 'vms'}})).json()"""

IPC_JS = """async ([cmd, args]) => {
  const ipc = window.__TAURI_INTERNALS__;
  if (!ipc || typeof ipc.invoke !== 'function') return {ok: false, error: 'sin __TAURI_INTERNALS__'};
  try { return {ok: true, value: await ipc.invoke(cmd, args || {})}; }
  catch (e) { return {ok: false, error: String(e && e.message ? e.message : e)}; }
}"""


# ============================================================================================ resultados
@dataclass
class Results:
    out: Path
    checks: list[dict[str, Any]] = field(default_factory=list)

    def run(self, name: str, fn: Callable[[], str]) -> bool:
        t0 = time.monotonic()
        print(f"== {name}", flush=True)
        try:
            detail = fn()
            ok = True
        except Exception as exc:  # noqa: BLE001 - se registra cualquier fallo de la comprobación
            detail = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            ok = False
        took = round(time.monotonic() - t0, 1)
        print(f"   {'OK' if ok else 'FALLO'} ({took} s): {detail}", flush=True)
        self.checks.append({"name": name, "ok": ok, "seconds": took, "detail": detail})
        self.save()
        return ok

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        data = {"when": dt.datetime.now(dt.timezone.utc).isoformat(), "ok": all(c["ok"] for c in self.checks),
                "checks": self.checks}
        (self.out / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def wait_for(check: Callable[[], Any], timeout: float, what: str, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(interval)
    raise TimeoutError(f"{what} (último valor: {last!r})")


def display_fp(hex_fp: str) -> str:
    return ":".join(hex_fp[i:i + 2] for i in range(0, len(hex_fp), 2)).upper()


# ============================================================================================ servidor HTTPS (S5)
@dataclass
class TlsCert:
    cert: Path
    key: Path
    sha256: str


def make_cert(folder: Path, name: str) -> TlsCert:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"vms-s5-{name}")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    folder.mkdir(parents=True, exist_ok=True)
    c, k = folder / f"{name}.crt", folder / f"{name}.key"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return TlsCert(c, k, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest())


class S5Server:
    """Hace de backend remoto con certificado autofirmado: /wall/1 → 302 → /destino (WebView2Feedback #4575)."""

    def __init__(self, port: int, cert: TlsCert) -> None:
        self.port, self.cert = port, cert
        self.requests: list[str] = []
        log = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - nombre de la API de http.server
                log.append(self.path)
                if self.path.startswith("/wall/"):
                    self.send_response(302)
                    self.send_header("Location", "/destino")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/api/health"):
                    body = b'{"status":"ok","version":"s5"}'
                    ctype = "application/json"
                else:
                    body = "<!doctype html><title>S5 correcto</title><h1>S5 correcto</h1>".encode()
                    ctype = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert.cert), str(cert.key))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "S5Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ============================================================================================ visor
class Viewer:
    def __init__(self, exe: Path, config_dir: Path, data_dir: Path) -> None:
        self.exe, self.config_dir, self.data_dir = exe, config_dir, data_dir
        self.proc: subprocess.Popen[bytes] | None = None
        self.env = {**os.environ, "VMS_VIEWER_CDP_PORT": str(CDP_PORT), "VMS_VIEWER_CONFIG_DIR": str(config_dir),
                    "VMS_DATA_DIR": str(data_dir)}

    def write_config(self, cfg: dict[str, Any]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "viewer.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def start(self, *args: str) -> None:
        self.proc = subprocess.Popen([str(self.exe), *args], env=self.env)
        wait_for(lambda: any(_cdp_open(p) for p in CDP_PORTS), 60, "el puerto CDP del visor no abrió")

    def run_second(self, *args: str) -> int:
        """Segunda instancia: pasa los argumentos a la primera y sale."""
        return subprocess.run([str(self.exe), *args], env=self.env, timeout=60).returncode

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
            self.proc.wait(timeout=20)
        self.proc = None

        wait_for(lambda: not any(_cdp_open(p, 0.5) for p in CDP_PORTS), 30, "el visor anterior sigue vivo")


class CdpError(RuntimeError):
    pass


def _cdp_open(port: int, timeout: float = 1.0) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def _targets() -> list[dict[str, Any]]:
    """Páginas de los dos navegadores del visor (muros y panel), cada una con el puerto CDP de su navegador."""
    out: list[dict[str, Any]] = []
    for port in CDP_PORTS:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5)
        except httpx.HTTPError:
            continue
        out.extend({**t, "port": port} for t in r.json() if t.get("type") == "page")
    return out


class Page:
    """Una página (un WebView) por CDP directo. Cada orden abre su propia conexión al objetivo, así sirve aunque
    la página cambie de origen (tauri.localhost → backend), cosa que Playwright con WebView2 no siguió (CI)."""

    def __init__(self, target_id: str, port: int = CDP_PORT) -> None:
        self.id = target_id
        self.port = port

    def _info(self) -> dict[str, Any]:
        for t in _targets():
            if t["id"] == self.id:
                return t
        raise CdpError(f"la página {self.id} ya no existe")

    @property
    def url(self) -> str:
        return str(self._info().get("url", ""))

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
        from websockets.sync.client import connect
        ws_url = self._info()["webSocketDebuggerUrl"]
        with connect(ws_url, max_size=None, open_timeout=10, close_timeout=2) as ws:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CdpError(f"{method}: sin respuesta en {timeout} s")
                msg = json.loads(ws.recv(timeout=left))
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise CdpError(f"{method}: {msg['error']}")
                    return dict(msg.get("result") or {})

    def evaluate(self, fn: str, arg: Any = None, timeout: float = 60) -> Any:
        expr = f"({fn})({json.dumps(arg)})"
        res = self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True},
                        timeout)
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            raise CdpError(f"excepción en la página: {d.get('exception', {}).get('description') or d.get('text')}")
        return res.get("result", {}).get("value")

    def wait_for_function(self, fn: str, timeout: float = 30000) -> Any:
        def check() -> Any:
            try:
                return self.evaluate(fn, None, 10)
            except CdpError:
                return None
        return wait_for(check, timeout / 1000, f"no se cumplió en la página: {fn[:80]}")

    def inner_text(self, selector: str) -> str:
        return str(self.evaluate("(s) => document.querySelector(s).textContent", selector))

    def goto(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})

    def screenshot(self, path: str) -> None:
        import base64
        res = self.send("Page.captureScreenshot", {"format": "png"})
        Path(path).write_bytes(base64.b64decode(res["data"]))


class Cdp:
    def connect(self) -> None:
        wait_for(lambda: _targets(), 30, "el visor no tiene páginas en CDP")

    def pages(self) -> list[Page]:
        return [Page(t["id"], t["port"]) for t in _targets()]

    def find(self, pred: Callable[[str], bool], timeout: float, what: str) -> Page:
        def look() -> Page | None:
            for t in _targets():
                if pred(str(t.get("url", ""))):
                    return Page(t["id"], t["port"])
            return None
        try:
            found: Page = wait_for(look, timeout, what)
            return found
        except TimeoutError as exc:
            urls = [t.get("url") for t in _targets()]
            raise TimeoutError(f"{exc}; páginas: {urls}") from None

    def stop(self) -> None:
        pass


# ============================================================================================ escenario
def start_backend(work: Path) -> tuple[Any, Any, str, str]:
    from tools.dev_run import (BackendClient, Service, base_env, free_port, python_cmd, seed_backend,
                               start_simulator, wait_until, write_env_file)

    simenv = start_simulator(work / "sim", hik_channels=2, dah_channels=1, people_cameras=0)
    kiosk = "kiosco-humo-" + os.urandom(8).hex()
    admin = "Admin#Humo-" + os.urandom(4).hex()
    port = free_port()
    mtx = os.environ.get("VMS_MEDIAMTX_BIN") or str(ROOT / "bin" / ("mediamtx.exe" if sys.platform == "win32"
                                                                    else "mediamtx"))
    values = {
        "VMS_DATA_DIR": str(work / "data"), "VMS_SITE_ID": "site-humo-001", "VMS_HTTP_HOST": "127.0.0.1",
        "VMS_HTTP_PORT": str(port), "VMS_ADMIN_INITIAL_PASSWORD": admin, "VMS_KIOSK_TOKEN": kiosk,
        "VMS_CREDENTIAL_BACKEND": "file", "VMS_LLM_PROVIDER": "none", "VMS_MEDIAMTX_BIN": mtx,
        "VMS_MTX_RTSP_ADDRESS": f"127.0.0.1:{free_port()}", "VMS_MTX_WEBRTC_ADDRESS": f"127.0.0.1:{free_port()}",
        "VMS_MTX_WEBRTC_ICE_UDP": f":{free_port()}", "VMS_MTX_WEBRTC_ICE_TCP": "off",
        "VMS_MTX_API_ADDRESS": f"127.0.0.1:{free_port()}", "VMS_MTX_PLAYBACK_ADDRESS": f"127.0.0.1:{free_port()}",
        "VMS_MTX_METRICS_ADDRESS": f"127.0.0.1:{free_port()}",
    }
    env_file = work / "humo.env"
    write_env_file(env_file, values)
    base = f"http://127.0.0.1:{port}"
    svc = Service("backend", python_cmd("-m", "vms"), base_env(env_file), work / "backend.log").start()
    wait_until(lambda: httpx.get(f"{base}/api/health", timeout=2).json()["engine"]["running"], 90,
               "backend con el motor en marcha")
    api = BackendClient(base, "admin", admin)
    seed_backend(api, simenv)
    api.close()
    secrets = work / "data" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "kiosk.token").write_text(kiosk + "\n", encoding="utf-8")
    return simenv, svc, base, admin


def current_user() -> str:
    return subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--exe", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--work", type=Path, default=None)
    args = ap.parse_args(argv)
    out: Path = args.out.resolve()
    work: Path = (args.work or out / "trabajo").resolve()
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    res = Results(out)
    simenv = svc = None
    viewer = Viewer(args.exe.resolve(), work / "visor-config", work / "data")
    cdp = Cdp()
    token = work / "data" / "secrets" / "kiosk.token"
    try:
        holder: dict[str, Any] = {}

        def stack() -> str:
            s, b, base, admin = start_backend(work)
            holder.update(sim=s, svc=b, base=base, admin=admin)
            return f"backend en {base}"
        if not res.run("pila: simulador + backend real con 3 cámaras", stack):
            return 1
        simenv, svc, base = holder["sim"], holder["svc"], holder["base"]

        # ------------------------------------------------------------------ 1. muro con vídeo (kiosco por archivo)
        def wall_with_frames() -> str:
            viewer.write_config({"schema": 1, "servers": [{"name": "local", "url": base}],
                                 "walls": [{"wall": 1, "server": "local"}]})
            viewer.start("--walls")
            cdp.connect()
            page = cdp.find(lambda u: u.startswith(base) and u.endswith("/wall/1"), 90, "el muro 1 no abrió")
            assert "token" not in page.url
            page.wait_for_function("() => { if (!window.__vmsWall) return false; "
                                   "const cams = window.__vmsWall.cells().filter(c => c.cameraId); "
                                   "return cams.length > 0 && cams.every(c => c.state === 'live'); }", timeout=60000)
            me = page.evaluate("() => fetch('/api/auth/me').then(r => r.json())")
            assert me.get("kiosk") is True, me
            r = page.evaluate(FRAMES_JS, 10000)
            page.screenshot(path=str(out / "muro-1.png"))
            assert r["cells"] > 0, r
            assert all(n >= FRAMES_MIN for n in r["rvfc"]), f"fotogramas por celda en 10 s: {r}"
            holder["wall"] = page
            return f"{r['cells']} celdas, fotogramas en 10 s: {r['rvfc']} (decodificados {r['decoded']})"
        res.run("1 · muro /wall/1 con fotogramas, entrando con kiosk.token (sin login)", wall_with_frames)

        def panel_apart_from_walls() -> str:
            wall = holder.get("wall") or cdp.find(lambda u: u.endswith("/wall/1"), 30, "muro")
            assert viewer.run_second("--panel") == 0
            panel = cdp.find(lambda u: u.startswith(base) and "/login" in u, 60, "el panel no abrió el login")
            assert panel.port != wall.port, "el panel y los muros deben ser navegadores (perfiles) distintos"
            st = panel.evaluate(LOGIN_JS, ["admin", holder["admin"]])
            assert st == 200, st
            # cada perfil solo tiene su cookie: con o sin la marca de muro, el panel es admin y el muro, kiosco
            who = {(name, hdr): page.evaluate(ME_JS, hdr) for name, page in (("panel", panel), ("muro", wall))
                   for hdr in (False, True)}
            assert all(v.get("username") == "admin" for (n, _), v in who.items() if n == "panel"), who
            assert all(v.get("kiosk") is True for (n, _), v in who.items() if n == "muro"), who
            try:
                wall.evaluate("() => location.reload()")
            except Exception:  # noqa: BLE001 - la recarga puede cortar la evaluación
                pass
            wall.wait_for_function("() => window.__vmsWall && window.__vmsWall.kiosk === true", timeout=30000)
            return f"panel en el puerto CDP {panel.port} (admin), muros en el {wall.port} (kiosco)"
        res.run("1 · panel y muros en perfiles de WebView2 separados (sesiones que no se pisan)", panel_apart_from_walls)

        def remote_page_has_no_ipc() -> str:
            page = holder.get("wall") or cdp.find(lambda u: u.endswith("/wall/1"), 30, "muro")
            outs = {cmd: page.evaluate(IPC_JS, [cmd, {}]) for cmd in ("diagnostico", "servidores", "acerca")}
            outs["plugin:window|close"] = page.evaluate(IPC_JS, ["plugin:window|close", {}])
            for cmd, r in outs.items():
                assert not r["ok"], f"{cmd} permitido desde el backend: {r}"
            return "; ".join(f"{c}: {r['error'][:80]}" for c, r in outs.items())
        res.run("5 · capacidades: la página del backend no puede usar IPC", remote_page_has_no_ipc)

        def local_page_has_ipc() -> str:
            assert viewer.run_second("--abrir", "diagnostico") == 0
            page = cdp.find(lambda u: "diagnostico.html" in u, 30, "la ventana de diagnóstico no abrió")
            r = page.evaluate(IPC_JS, ["acerca", {}])
            assert r["ok"] and r["value"]["producto"] == "VMS Multimarca", r
            page.wait_for_function("() => document.getElementById('srv-estado').textContent !== 'Comprobando…'",
                                   timeout=20000)
            page.screenshot(path=str(out / "diagnostico.png"))
            return f"segunda instancia reenviada; acerca() = versión {r['value']['version']}"
        res.run("5 · capacidades: una página local sí usa los comandos del visor", local_page_has_ipc)
        viewer.stop()

        # ------------------------------------------------------------------ 3. sin permiso para kiosk.token
        def without_permission() -> str:
            user = current_user()
            subprocess.run(["icacls", str(token), "/deny", f"{user}:(R)"], check=True, capture_output=True)
            try:
                try:
                    token.read_text(encoding="utf-8")
                    raise AssertionError("el usuario todavía puede leer kiosk.token")
                except PermissionError:
                    pass
                viewer.start("--walls")
                cdp.connect()
                page = cdp.find(lambda u: "sin-permiso.html" in u, 60, "no apareció «sin permiso»")
                page.wait_for_function("() => document.getElementById('titulo').textContent.includes('Sin permiso')",
                                       timeout=15000)
                title = page.inner_text("#titulo")
                page.screenshot(path=str(out / "sin-permiso.png"))
                assert not any(p.url.startswith(base) for p in cdp.pages()), "no debe abrir nada del backend"
                return f"{user} sin lectura → «{title}»"
            finally:
                subprocess.run(["icacls", str(token), "/remove:d", user], capture_output=True)
                viewer.stop()
        res.run("3 · usuario sin permiso sobre kiosk.token → «Sin permiso para abrir los muros»", without_permission)

        # ------------------------------------------------------------------ 4. S5: fijación del certificado
        s5_port = httpx_free_port()
        cert_a = make_cert(work / "s5", "a")
        cert_b = make_cert(work / "s5", "b")
        s5 = {"srv": S5Server(s5_port, cert_a).start()}
        origin = f"https://127.0.0.1:{s5_port}"

        def s5_pinned_loads() -> str:
            viewer.write_config({"schema": 1, "servers": [{"name": "remoto", "url": origin, "sha256": cert_a.sha256}],
                                 "walls": [{"wall": 1, "server": "remoto"}]})
            viewer.start("--walls")
            cdp.connect()
            page = cdp.find(lambda u: u.startswith(origin) and u.endswith("/destino"), 60,
                            "la página del servidor con certificado fijado no cargó")
            page.wait_for_function("() => document.title === 'S5 correcto'", timeout=15000)
            holder["s5page"] = page
            assert "/wall/1" in s5["srv"].requests, s5["srv"].requests
            return f"cargó tras la redirección 302 /wall/1 → /destino ({len(s5['srv'].requests)} peticiones)"
        res.run("6 · S5: certificado autofirmado con la huella fijada → carga (con redirección 302)", s5_pinned_loads)

        def s5_reconnect() -> str:
            s5["srv"].stop()
            time.sleep(2)
            s5["srv"] = S5Server(s5_port, cert_a).start()
            page = holder["s5page"]
            page.goto(f"{origin}/wall/1")
            page.wait_for_function("() => document.title === 'S5 correcto'", timeout=20000)
            return "el servidor reiniciado (mismo certificado) vuelve a cargar"
        res.run("6 · S5: reconexión tras reiniciar el servidor", s5_reconnect)

        def s5_changed_cert_warns() -> str:
            s5["srv"].stop()
            time.sleep(1)
            s5["srv"] = S5Server(s5_port, cert_b).start()
            page = holder["s5page"]
            try:
                page.evaluate("() => location.reload()")
            except Exception:  # noqa: BLE001 - la navegación cancelada puede cortar la evaluación
                pass
            warn = cdp.find(lambda u: "certificado.html" in u, 30, "no apareció el aviso de certificado cambiado")
            warn.wait_for_function("() => !document.getElementById('cambiado').hidden", timeout=15000)
            info = warn.evaluate(IPC_JS, ["certificado", {}])
            assert info["ok"], info
            v = info["value"]
            assert v["observada"] == display_fp(cert_b.sha256) and v["esperada"] == display_fp(cert_a.sha256), v
            warn.screenshot(path=str(out / "certificado-cambiado.png"))
            served = [p for p in s5["srv"].requests if p.startswith("/destino")]
            assert served == [], f"el servidor con el certificado nuevo sirvió páginas: {served}"
            return "aviso con las dos huellas; el servidor con el certificado nuevo no sirvió ninguna página"
        res.run("6 · S5: certificado cambiado (sesión abierta) → aviso y no carga", s5_changed_cert_warns)

        def s5_changed_cert_at_start() -> str:
            viewer.stop()
            s5["srv"].requests.clear()
            viewer.start("--walls")
            cdp.connect()
            cdp.find(lambda u: "certificado.html" in u, 60, "no apareció el aviso al arrancar")
            assert not any(p.startswith("/wall") or p.startswith("/destino") for p in s5["srv"].requests), \
                s5["srv"].requests
            return "al arrancar, la comprobación previa del visor detecta el cambio antes de pedir nada"
        res.run("6 · S5: certificado cambiado al arrancar → aviso sin abrir la página", s5_changed_cert_at_start)
        s5["srv"].stop()
    finally:
        viewer.stop()
        cdp.stop()
        for src, dst in ((viewer.config_dir / "visor.log", out / "visor.log"), (work / "backend.log", out / "backend.log")):
            if src.is_file():
                shutil.copy(src, dst)
        if svc is not None:
            svc.stop()
        if simenv is not None:
            simenv.stop()
        res.save()
    failed = [c["name"] for c in res.checks if not c["ok"]]
    print(f"\n{len(res.checks) - len(failed)}/{len(res.checks)} comprobaciones OK" +
          (f"; fallan: {failed}" if failed else ""))
    return 1 if failed else 0


def httpx_free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
