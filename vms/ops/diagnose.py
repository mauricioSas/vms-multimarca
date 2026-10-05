"""«¿Por qué no conecta?»: reglas deterministas encadenadas (CONTRATO §18.11).

Orden: `ping` → `tcp_http` → `tcp_rtsp` → `http_response` → `auth` → `lockout` → `rtsp_describe` → `codec` →
`clock` → `engine` → `path_ready`. Cada paso trae la causa probable y qué hacer, en lenguaje de tienda.

**Un solo intento con credenciales** si la contraseña puede ser mala: el paso `auth` hace UNA petición
autenticada a la API; si falla, no se vuelve a probar la contraseña por ninguna vía (ni por RTSP), porque
Hikvision y Dahua bloquean el usuario tras varios fallos (`LockoutPolicy`). Solo con la contraseña ya
comprobada se hacen las lecturas siguientes (RTSP, hora).

El LLM es opcional (`VMS_LLM_*`) y solo reescribe `summary_es` a partir de los pasos ya calculados, sin IP,
usuarios ni contraseñas (`llm_used`). Si falla, se queda el texto de las reglas.

`ping` no usa ICMP (necesitaría privilegios): comprueba que el equipo acepta conexiones en alguno de sus
puertos conocidos.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from vms.core.errors import (DeviceAuthFailed, DeviceError, DeviceProtocolError, DeviceUnreachable,
                             DeviceUnsupported)
from vms.core.interfaces import DeviceClient, DeviceClockClient, PathStatus
from vms.core.models import Camera, Device, DeviceBase
from vms.core.rtsp import preset_paths, redact
from vms.core.sources import camera_paths

from . import drivers
from .health.clock import device_check
from .host import dynamic
from .models import DiagnosisResult, DiagnosisStep

log = logging.getLogger("vms.ops.diagnose")

# Política de bloqueo por marca mientras el registro de drivers (B5) no la exponga.
FALLBACK_LOCKOUT: dict[str, tuple[int, int]] = {"hikvision": (5, 30), "dahua": (5, 30)}
VENDOR_NAMES = {"hikvision": "Hikvision", "dahua": "Dahua", "onvif": "El equipo", "generic": "El equipo"}

TcpCheck = Callable[[str, int, float], Awaitable[bool]]
RtspProbe = Callable[..., Awaitable[Any]]
HttpProbe = Callable[[str, int, bool, float], Awaitable[int | None]]
Rewriter = Callable[[str, list[DiagnosisStep]], Awaitable[str]]


def lockout_policy(vendor: str) -> tuple[int, int] | None:
    try:   # registro de drivers de B5 (si ya está entregado)
        get_driver = dynamic("vms.vendors.registry", "get_driver")
        drv = get_driver(vendor)
        if drv is not None and drv.lockout is not None:
            return int(drv.lockout.attempts), int(drv.lockout.minutes)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        log.debug("Registro de drivers no disponible", exc_info=True)
    return FALLBACK_LOCKOUT.get(vendor)


async def _tcp(host: str, port: int, timeout: float) -> bool:
    ok: bool = await dynamic("vms.vendors.rtsp_probe", "tcp_reachable")(host, port, timeout)
    return ok


async def _http_status(host: str, port: int, https: bool, timeout: float) -> int | None:
    """Estado HTTP de `GET /` SIN credenciales (None si no hay respuesta HTTP)."""
    from vms.core.rtsp import format_host
    url = f"{'https' if https else 'http'}://{format_host(host)}:{port}/"
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=False) as c:  # noqa: S501
            r = await c.get(url)
            return r.status_code
    except httpx.HTTPError:
        return None


async def _rtsp(host: str, port: int, path: str, username: str = "", password: str = "", *,
                timeout: float = 5.0) -> Any:
    return await dynamic("vms.vendors.rtsp_probe", "probe_rtsp")(host, port, path, username, password, timeout=timeout)


@dataclass
class DiagnoseInput:
    device: DeviceBase
    password: str
    device_id: str | None = None
    cameras: list[Camera] = field(default_factory=list)     # cámaras de ese equipo (o la indicada)
    camera_id: str | None = None


class Diagnoser:
    def __init__(self, client_factory: Callable[[Device, str], DeviceClient], *,
                 engine_running: Callable[[], Awaitable[bool]] | None = None,
                 paths_status: Callable[[], Awaitable[dict[str, PathStatus] | None]] | None = None,
                 tcp_check: TcpCheck = _tcp, http_probe: HttpProbe = _http_status, rtsp_probe: RtspProbe = _rtsp,
                 clock_thresholds: tuple[float, float] = (2.0, 30.0), rewriter: Rewriter | None = None,
                 timeout: float = 3.0) -> None:
        self.client_factory = client_factory
        self.engine_running = engine_running
        self.paths_status = paths_status
        self.tcp_check = tcp_check
        self.http_probe = http_probe
        self.rtsp_probe = rtsp_probe
        self.clock_thresholds = clock_thresholds
        self.rewriter = rewriter
        self.timeout = timeout

    async def run(self, inp: DiagnoseInput) -> DiagnosisResult:
        d = inp.device
        vendor_name = VENDOR_NAMES.get(d.vendor, "El equipo")
        steps: list[DiagnosisStep] = []

        def add(code: str, ok: bool | None, detail: str, action: str = "", t0: float | None = None) -> DiagnosisStep:
            s = DiagnosisStep(code=code, ok=ok, detail_es=detail, action_es=action,
                              elapsed_ms=round((time.perf_counter() - t0) * 1000, 1) if t0 else 0.0)
            steps.append(s)
            return s

        def skip(*codes: str, why: str = "No se pudo comprobar porque falló un paso anterior.") -> None:
            for c in codes:
                add(c, None, why)

        onvif_port = d.onvif_port or d.http_port
        # 1-3) red
        t0 = time.perf_counter()
        http_ok = await self.tcp_check(d.host, d.http_port, self.timeout)
        rtsp_ok = await self.tcp_check(d.host, d.rtsp_port, self.timeout)
        onvif_ok = http_ok if onvif_port == d.http_port else await self.tcp_check(d.host, onvif_port, self.timeout)
        alive = http_ok or rtsp_ok or onvif_ok
        add("ping", alive, "El equipo responde en la red." if alive else "El equipo no responde en la red.",
            "" if alive else "Comprueba que está encendido, el cable de red (o el puerto PoE del grabador) y que la "
                             "IP escrita es la del equipo. Si cambió de IP, búscalo de nuevo con «Buscar en la red».",
            t0)
        if not alive:
            skip("tcp_http", "tcp_rtsp", "http_response", "auth", "lockout", "rtsp_describe", "codec", "clock")
            await self._engine_steps(inp, add)
            return await self._finish(inp, steps)
        add("tcp_http", http_ok, f"La web del equipo (puerto {d.http_port}) acepta conexiones." if http_ok else
            f"El puerto web {d.http_port} no responde.",
            "" if http_ok else "Revisa el puerto HTTP en el alta del equipo (normalmente 80) o si la web está "
                               "desactivada en el equipo.")
        add("tcp_rtsp", rtsp_ok, f"El puerto de vídeo RTSP ({d.rtsp_port}) acepta conexiones." if rtsp_ok else
            f"El puerto de vídeo RTSP {d.rtsp_port} no responde.",
            "" if rtsp_ok else "Activa RTSP en el equipo o corrige el puerto RTSP (normalmente 554). Algunos "
                               "firmwares nuevos lo traen desactivado.")
        # 4) respuesta HTTP sin credenciales
        t0 = time.perf_counter()
        status = await self.http_probe(d.host, d.http_port, d.https, self.timeout) if http_ok else None
        if not http_ok:
            add("http_response", None, "Sin puerto web no se puede comprobar la respuesta.")
        elif status is None:
            add("http_response", False, "El puerto web acepta conexiones pero no contesta como una web.",
                "Puede que el puerto sea de otro servicio o que haya que usar HTTPS: marca «Usar HTTPS» y prueba "
                "de nuevo.", t0)
        else:
            add("http_response", True, f"La web del equipo contesta (código {status}).", "", t0)
        # 5-6) credenciales: UN intento
        auth_ok: bool | None = None
        client: DeviceClient | None = None
        if not drivers.has_api(d.vendor):
            add("auth", None, f"Los equipos «{drivers.driver_name(d.vendor)}» no tienen API: la contraseña se prueba "
                              "con el vídeo RTSP.")
        elif not (http_ok or onvif_ok):
            add("auth", None, "Sin puerto web no se puede probar la contraseña por la API.")
        else:
            t0 = time.perf_counter()
            dev = d if isinstance(d, Device) else Device(**d.model_dump())
            try:
                client = self.client_factory(dev, inp.password)
                info = await client.probe()
                auth_ok = True
                model = f" ({info.model})" if info.model else ""
                add("auth", True, f"Usuario y contraseña correctos{model}.", "", t0)
            except DeviceAuthFailed:
                auth_ok = False
                add("auth", False, "El equipo responde pero rechaza el usuario o la contraseña.",
                    "Comprueba la contraseña en la web del equipo y escríbela de nuevo en el alta. Usa el usuario de "
                    "solo lectura creado para el VMS.", t0)
            except DeviceUnreachable as exc:
                auth_ok = None
                add("auth", False, f"La API del equipo no contesta: {redact(exc.message)}",
                    "Revisa el puerto HTTP y que la marca elegida sea la correcta.", t0)
            except DeviceUnsupported as exc:
                add("auth", None, redact(exc.message), "", t0)
            except DeviceProtocolError as exc:
                add("auth", False, f"El equipo contestó algo que no se esperaba: {redact(exc.message)}",
                    "Comprueba que la marca elegida es la del equipo (o prueba con «ONVIF (otras marcas)»).", t0)
            except DeviceError as exc:
                add("auth", False, redact(exc.message), "", t0)
        policy = lockout_policy(d.vendor)
        if auth_ok is False:
            if policy:
                add("lockout", False, f"No se volverá a probar la contraseña: tras {policy[0]} intentos fallidos "
                                      f"{vendor_name} bloquea el usuario {policy[1]} minutos.",
                    "Corrige la contraseña antes de volver a probar. Si ya está bloqueado, espera o reinicia el equipo.")
            else:
                add("lockout", False, "No se volverá a probar la contraseña: muchos equipos bloquean el usuario tras "
                                      "varios intentos fallidos.", "Corrige la contraseña antes de volver a probar.")
        elif auth_ok:
            add("lockout", True, "Sin riesgo de bloqueo: la contraseña es correcta.")
        else:
            add("lockout", None, "No se probó la contraseña por la API.")
        # 7-8) RTSP y códec: solo con la contraseña ya comprobada (o si el equipo no tiene API)
        main_path, sub_path = self._paths(inp)
        codec_main = codec_sub = None
        if auth_ok is False:
            skip("rtsp_describe", "codec", why="No se prueba el vídeo para no gastar otro intento de contraseña.")
        elif not rtsp_ok:
            skip("rtsp_describe", "codec", why="El puerto RTSP no responde.")
        elif not main_path:
            add("rtsp_describe", None, "No se conoce la ruta RTSP de este equipo.",
                "Escribe la ruta del flujo principal en la cámara (la indica el fabricante).")
            add("codec", None, "Sin ruta RTSP no se puede saber el códec.")
        else:
            t0 = time.perf_counter()
            r = await self.rtsp_probe(d.host, d.rtsp_port, main_path, d.username, inp.password, timeout=self.timeout + 2)
            if r.status == 200:
                codec_main = r.video_codec
                add("rtsp_describe", True, f"El vídeo responde ({codec_main or 'códec desconocido'}).", "", t0)
            elif r.status == 401:
                add("rtsp_describe", False, "El vídeo (RTSP) rechaza el usuario o la contraseña.",
                    "Usa el mismo usuario y contraseña que en la web del equipo; revisa que el usuario tenga "
                    "permiso de vídeo en directo.", t0)
            elif r.status == 404:
                add("rtsp_describe", False, "El equipo no tiene vídeo en esa ruta RTSP.",
                    "Revisa el canal o escribe la ruta correcta del flujo principal.", t0)
            elif not r.reachable:
                add("rtsp_describe", False, "No se pudo abrir el vídeo RTSP.", "Revisa el puerto RTSP.", t0)
            else:
                add("rtsp_describe", False, f"El vídeo respondió con el código {r.status}: {redact(r.error)}".strip(": "),
                    "", t0)
            if r.status == 200 and sub_path:
                rs = await self.rtsp_probe(d.host, d.rtsp_port, sub_path, d.username, inp.password,
                                           timeout=self.timeout + 2)
                codec_sub = rs.video_codec if rs.status == 200 else None
            wall_codec = codec_sub or codec_main
            if r.status != 200:
                add("codec", None, "Sin vídeo no se puede saber el códec.")
            elif wall_codec and "265" in wall_codec:
                add("codec", False, f"El {'subflujo' if codec_sub else 'flujo'} va en H.265: el navegador y los muros "
                                    "no siempre pueden mostrarlo.",
                    "En la web de la cámara, cambia el subflujo (flujo secundario) a H.264. La grabación puede seguir "
                    "en H.265.")
            else:
                add("codec", True, f"Códec compatible con los muros ({wall_codec or 'no informado'}).")
        # 9) hora
        if auth_ok and client is not None and drivers.TIME_READ in drivers.capabilities(d.vendor) \
                and isinstance(client, DeviceClockClient):
            t0 = time.perf_counter()
            try:
                dt = await client.device_time()
                chk = device_check(inp.device_id or "dev-diag", None, dt, *self.clock_thresholds)
                add("clock", chk.status == "ok", chk.message_es,
                    "" if chk.status == "ok" else "Activa NTP en el equipo apuntando a este PC.", t0)
            except DeviceError as exc:
                add("clock", None, f"No se pudo leer la hora: {redact(exc.message)}", "", t0)
        else:
            add("clock", None, "La hora de este equipo no se puede leer desde aquí (marca sin lectura de hora o "
                               "contraseña sin comprobar).")
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                log.debug("Error cerrando el cliente", exc_info=True)
        await self._engine_steps(inp, add)
        return await self._finish(inp, steps)

    def _paths(self, inp: DiagnoseInput) -> tuple[str | None, str | None]:
        d = inp.device
        cams = inp.cameras
        if cams:
            dev = d if isinstance(d, Device) else Device(**d.model_dump())
            try:
                main, sub = camera_paths(dev, cams[0])
            except ValueError:
                return None, None
            return main or None, (sub if cams[0].has_sub else None)
        preset = preset_paths(d.vendor, 1) if d.vendor in ("hikvision", "dahua") else None
        return (preset[0], preset[1]) if preset else (None, None)

    async def _engine_steps(self, inp: DiagnoseInput, add: Callable[..., DiagnosisStep]) -> None:
        running = await self.engine_running() if self.engine_running else None
        if running is None:
            add("engine", None, "No se pudo consultar el motor de vídeo.")
        elif running:
            add("engine", True, "El motor de vídeo del VMS está en marcha.")
        else:
            add("engine", False, "El motor de vídeo del VMS no está en marcha.",
                "Reinicia el servicio del VMS desde «Estado del sistema» o reinicia el PC.")
        if not inp.cameras:
            add("path_ready", None, "Este equipo aún no tiene cámaras dadas de alta en el VMS.")
            return
        paths = await self.paths_status() if self.paths_status else None
        if paths is None or not running:
            add("path_ready", None, "No se pudo saber si llega el vídeo al VMS.")
            return
        bad = []
        for cam in inp.cameras:
            st = paths.get(f"{cam.id}/main")
            if not (st and st.ready):
                err = f" ({redact(st.last_error)})" if st and st.last_error else ""
                bad.append(f"{cam.name}{err}")
        if bad:
            add("path_ready", False, "No llega vídeo al VMS de: " + ", ".join(bad[:8]) + ("…" if len(bad) > 8 else ""),
                "Si los pasos anteriores están bien, espera un minuto: el VMS reintenta solo. Si sigue, revisa la ruta "
                "RTSP de esas cámaras.")
        else:
            add("path_ready", True, "El vídeo de todas sus cámaras llega al VMS.")

    async def _finish(self, inp: DiagnoseInput, steps: list[DiagnosisStep]) -> DiagnosisResult:
        failed = [s for s in steps if s.ok is False]
        if not failed:
            cause = "No se ha encontrado ningún problema."
            summary = "Todo correcto: el equipo responde, la contraseña es válida y el vídeo llega al VMS."
        else:
            first = failed[0]
            cause = first.detail_es
            summary = f"{first.detail_es} {first.action_es}".strip()
            if len(failed) > 1:
                summary += f" (Hay {len(failed) - 1} {'aviso más' if len(failed) == 2 else 'avisos más'} abajo.)"
        result = DiagnosisResult(device_id=inp.device_id, camera_id=inp.camera_id, steps=steps,
                                 probable_cause_es=cause, summary_es=summary)
        if self.rewriter is not None and failed:
            # al LLM solo va texto saneado: sin IP, credenciales, ni la dirección o el nombre del equipo (un host
            # DNS o un nombre como «Caja Gran Vía 32» no los tapa la expresión de IP)
            private = (inp.device.host, inp.device.name)
            try:
                text = await self.rewriter(sanitize_text(summary, private),
                                           [sanitize_step(s, private) for s in steps])
                if text and len(text) < 1500:
                    result.summary_es = sanitize_text(text, private)
                    result.llm_used = True
            except Exception as exc:  # noqa: BLE001 - el LLM es opcional
                log.info("El LLM no pudo redactar el diagnóstico: %s", type(exc).__name__)
        return result


_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\[[0-9a-fA-F:]+\]")


def sanitize_text(text: str, private: tuple[str, ...] = ()) -> str:
    """Sin IP ni credenciales (lo que viaja al LLM y lo que vuelve). `private`: textos literales que tampoco
    salen (dirección y nombre del equipo): la dirección pasa a «<IP>» y el resto a «el equipo»."""
    out = redact(text)
    host = private[0].strip() if private else ""
    for literal in sorted({p.strip() for p in private if p and p.strip()}, key=len, reverse=True):
        out = re.sub(re.escape(literal), "<IP>" if literal == host else "el equipo", out, flags=re.IGNORECASE)
    return _IP_RE.sub("<IP>", out)


def sanitize_step(s: DiagnosisStep, private: tuple[str, ...] = ()) -> DiagnosisStep:
    return s.model_copy(update={"detail_es": sanitize_text(s.detail_es, private),
                                "action_es": sanitize_text(s.action_es, private)})


def llm_rewriter_from_settings(settings: Any) -> Rewriter | None:
    """Reescritura opcional con el proveedor LLM ya configurado para el informe semanal (VMS_LLM_*)."""
    if getattr(settings, "llm_provider", "none") == "none" or not getattr(settings, "llm_api_key", None):
        return None
    try:
        provider = dynamic("analytics.reports.llm", "provider_from_settings")(settings)
    except ImportError:
        return None
    except Exception:  # noqa: BLE001
        return None
    if provider is None:
        return None

    async def rewrite(summary: str, steps: list[DiagnosisStep]) -> str:
        lines = "\n".join(f"- {s.code}: {'bien' if s.ok else 'mal' if s.ok is False else 'sin comprobar'} — "
                          f"{s.detail_es} {s.action_es}" for s in steps)
        system = ("Redactas en español neutro con tuteo, para el encargado de una tienda sin conocimientos técnicos, "
                  "la explicación de por qué una cámara no conecta. Usa SOLO los hechos de la lista; no inventes "
                  "datos, no incluyas direcciones IP, usuarios ni contraseñas. Máximo 4 frases: qué pasa y qué hacer.")
        prompt = f"Resumen de las reglas: {summary}\nPasos:\n{lines}"
        return str(await provider.complete(system, prompt, max_tokens=300)).strip()

    return rewrite
