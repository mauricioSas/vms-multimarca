"""Arranque del servidor web del backend: HTTP y, si hay certificado, HTTPS para la LAN.

Sin certificado: un único servidor HTTP en VMS_HTTP_HOST:VMS_HTTP_PORT (como siempre). Si escucha
fuera de 127.0.0.1 se avisa en el registro: el login y las contraseñas de los equipos viajarían
sin cifrar por la LAN de la tienda.

Con certificado (VMS_TLS_CERT_FILE + VMS_TLS_KEY_FILE):
  - HTTPS en VMS_HTTP_HOST:VMS_HTTPS_PORT para los navegadores de la LAN;
  - HTTP SOLO en 127.0.0.1:VMS_HTTP_PORT para lo que corre en el mismo PC (muros en kiosco,
    analítica, agente de latido, comprobación del instalador), que no sale del equipo.
Los dos sirven la misma aplicación; el arranque y la parada (lifespan: motor, latido...) solo
los hace el servidor HTTP. La cookie de sesión lleva `Secure` cuando la petición llega por HTTPS.

Parada (CONTRATO §14.1): desde que `vmsctl run` cierra la entrada estándar hasta que mata el proceso pasan los
`grace_s` de VMSBackend (`native/common/src/services.rs`). Ahí tienen que caber la espera de uvicorn a las
conexiones abiertas (`GRACEFUL_SHUTDOWN_S`), la parada de la app (aplicación pendiente al motor,
`STOP_FLUSH_APPLY_S` en app.py; motor y proxy) y la salida del proceso. Los flujos SSE terminan en cuanto empieza
la parada (`AppState.begin_stop`): antes retenían a uvicorn sus 10 s enteros y vmsctl mataba el backend sin que
llegara a pararse. `tests/api/test_shutdown.py` comprueba las cuentas contra el catálogo de servicios.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import Callable, Iterator
from typing import Any

import uvicorn

from vms.core.settings import VmsSettings

log = logging.getLogger("vms.api.serve")

# Espera máxima a que terminen las peticiones en curso al parar (p. ej. la descarga de una grabación).
GRACEFUL_SHUTDOWN_S = 3

COMMON: dict[str, Any] = {"log_config": None, "access_log": False, "server_header": False, "proxy_headers": False,
                          "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_S}


class _SecondaryServer(uvicorn.Server):
    """Servidor HTTPS: no captura señales (las gestiona el principal) y se para con él."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host == "localhost"


def build_configs(app: Any, settings: VmsSettings) -> tuple[uvicorn.Config, uvicorn.Config | None]:
    """(config HTTP, config HTTPS o None). Público para las pruebas."""
    if not settings.tls_enabled:
        return uvicorn.Config(app, host=settings.http_host, port=settings.http_port, **COMMON), None
    http = uvicorn.Config(app, host="127.0.0.1", port=settings.http_port, **COMMON)
    https = uvicorn.Config(app, host=settings.http_host, port=settings.https_port, lifespan="off",
                           ssl_certfile=str(settings.tls_cert_file), ssl_keyfile=str(settings.tls_key_file),
                           **COMMON)
    return http, https


async def _run_secondary(server: uvicorn.Server) -> None:
    try:
        await server.serve()
    except SystemExit:   # uvicorn sale así si no puede abrir el puerto o leer el certificado
        log.error("No se pudo arrancar HTTPS en el puerto %s (¿puerto ocupado o certificado no válido?). "
                  "La web sigue disponible solo desde este PC en http://127.0.0.1", server.config.port)
    except Exception:  # noqa: BLE001 - el servidor principal debe seguir aunque falle HTTPS
        log.exception("El servidor HTTPS se detuvo por un error")


def _stop_hook(app: Any) -> Callable[[], None]:
    """`AppState.begin_stop` de la app (en las pruebas puede no haberla)."""
    begin = getattr(getattr(getattr(app, "state", None), "vms", None), "begin_stop", None)
    return begin if callable(begin) else (lambda: None)


async def _follow(primary: uvicorn.Server, secondary: uvicorn.Server | None, on_stop: Callable[[], None]) -> None:
    """En cuanto el principal recibe la orden de parada: avisa a la app (los flujos SSE terminan, en vez de
    retener a uvicorn todo su plazo) y para el HTTPS (antes de que uvicorn vuelva a lanzar la señal al terminar,
    que cerraría el proceso de golpe)."""
    while not primary.should_exit:
        await asyncio.sleep(0.1)
    try:
        on_stop()
    except Exception:  # noqa: BLE001 - la parada sigue aunque falle el aviso
        log.exception("No se pudo avisar a la aplicación de que el servidor se para")
    if secondary is not None:
        secondary.should_exit = True


async def serve(app: Any, settings: VmsSettings) -> bool:
    """Sirve hasta recibir la señal de parada. False si el servidor principal no llegó a arrancar."""
    http_cfg, https_cfg = build_configs(app, settings)
    primary = uvicorn.Server(http_cfg)
    secondary_task: asyncio.Task[None] | None = None
    secondary: uvicorn.Server | None = None
    if https_cfg is not None:
        secondary = _SecondaryServer(https_cfg)
        secondary_task = asyncio.create_task(_run_secondary(secondary), name="https")
        log.info("Web por HTTPS en %s:%s; HTTP solo en 127.0.0.1:%s", settings.http_host, settings.https_port,
                 settings.http_port)
    elif not _is_loopback(settings.http_host):
        log.warning("La web se sirve por HTTP sin cifrar en %s:%s: en la LAN, el login y las contraseñas de los "
                    "equipos viajan en claro. Activa HTTPS (python -m vms tls-cert) o accede solo por la VPN "
                    "(docs/RED.md)", settings.http_host, settings.http_port)
    follower = asyncio.create_task(_follow(primary, secondary, _stop_hook(app)), name="stop-follower")
    try:
        await primary.serve()
    finally:
        if secondary is not None and secondary_task is not None:
            secondary.should_exit = True
            with contextlib.suppress(asyncio.CancelledError):
                await secondary_task
        follower.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await follower
    return primary.started


def run(app: Any, settings: VmsSettings) -> bool:
    # Bucle por defecto (Proactor en Windows): el motor lanza MediaMTX como subproceso.
    return asyncio.run(serve(app, settings))
