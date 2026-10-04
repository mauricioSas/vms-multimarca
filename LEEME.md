# VMS Multimarca

Programa de videovigilancia para cámaras y grabadores (NVR) **Hikvision, Dahua y ONVIF**
mezclados, sin licencia por cámara:

- **Vista en vivo en hasta 4 monitores** (muros de 1, 4, 9 o 16 cámaras; doble clic para ver
  una cámara a pantalla completa).
- **Grabación 24/7** con retención por días y protección de disco.
- **Reproducción** con línea de tiempo y descarga de clips.
- **Analítica de tienda (opcional):** conteo anónimo de personas que entran por la puerta
  (cruce de línea) y ocupación de la cola de cajas, con avisos por Telegram, conteos por minuto en
  PostgreSQL e **informe semanal por tienda**.
- **Panel central multi-tienda:** estado de todas las sedes (en línea / caída), conteos de hoy y
  de la semana, comparativa entre sedes, colas con más alertas e informes semanales.

La analítica no guarda imágenes ni vídeo, no identifica personas y no mide a trabajadores: solo
guarda conteos anónimos (ver [docs/RGPD-EIPD.md](docs/RGPD-EIPD.md)).

## Arquitectura

```
  Cámaras IP / NVR Hikvision · Dahua · ONVIF          (VLAN de cámaras, sin salida a Internet)
            │ RTSP: UNA conexión por canal (flujo principal 24/7 + subflujo bajo demanda)
            ▼
 ┌──────────────────────── PC de la sede (Windows 10/11 o Linux) ─────────────────────────┐
 │                                                                                        │
 │  Servicio VMS  (python -m vms, puerto 8600)                                            │
 │   ├─ API REST + interfaz web (panel, muros, reproducción, dibujo de zonas)             │
 │   ├─ alta de equipos: ISAPI Hikvision · CGI/RPC2 Dahua · ONVIF · descubrimiento        │
 │   └─ supervisa ──► MediaMTX (127.0.0.1)                                                │
 │                     ├─ graba 24/7 en fMP4, borra lo antiguo, reproduce (/list, /get)   │
 │                     ├─ WebRTC/WHEP ──► navegadores (ICE 8189/udp)                      │
 │                     └─ RTSP local ──► analítica                                        │
 │                                                                                        │
 │  Analítica (opcional, python -m analytics)                                             │
 │   RF-DETR (ONNX/OpenVINO) → seguimiento ByteTrack → línea de puerta / zona de cola     │
 │   → conteos por minuto ──────────────────────────────┐   → avisos Telegram            │
 │                                                      │                                 │
 │  Latido (dentro del servicio VMS o python -m central.agent)                            │
 └──────────────────────────────────────────────────────┼─────────────────────────────────┘
       ▲ Edge/Chrome en modo kiosco,                      │  VPN Headscale/Tailscale
       │ una ventana por monitor (/wall/1..4)             ▼  (la tienda sale; no se abren puertos)
                                          ┌──────────── Servidor central ─────────────┐
                                          │ PostgreSQL (conteos, alertas, latidos)    │
                                          │ Panel central (python -m central, 8700)   │
                                          │ Informe semanal (lunes 06:00, proveedor   │
                                          │ LLM configurable que solo redacta cifras) │
                                          └───────────────────────────────────────────┘
```

Detalle de interfaces, puertos, API y esquema SQL: [docs/CONTRATO.md](docs/CONTRATO.md).
Plan y decisiones: [PLAN.md](PLAN.md). Estado actual, qué está probado y qué falta por probar con
equipos reales: [docs/ESTADO.md](docs/ESTADO.md).

## Inicio rápido en Windows (PC de control con 4 monitores)

1. Copia la carpeta del programa al PC (por ejemplo `C:\Instalacion\vms-multimarca`).
2. Abre **PowerShell como administrador** en esa carpeta y ejecuta:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\deploy\windows\install.ps1                       # solo vídeo
   # o, tienda con analítica y latido a la central:
   .\deploy\windows\install.ps1 -Components Backend,Analytics,Heartbeat -SiteId site-bcn-001 `
       -CentralUrl https://central.vpn:8700
   ```
3. Abre `http://127.0.0.1:8600/` en ese PC y crea el primer administrador.
4. Da de alta los NVR y cámaras ([docs/ALTA-EQUIPOS.md](docs/ALTA-EQUIPOS.md)) y organiza los muros.
5. Muros en los 4 monitores: `.\deploy\windows\install-kiosk.ps1 -KioskUser 'PC-CONTROL\muros'`.

Guía completa paso a paso: [docs/INSTALACION-WINDOWS.md](docs/INSTALACION-WINDOWS.md).
Mini PC de tienda con Linux: [docs/INSTALACION-LINUX.md](docs/INSTALACION-LINUX.md).
Panel central: [docs/PANEL-CENTRAL.md](docs/PANEL-CENTRAL.md).

## Inicio rápido para desarrollo (macOS o Linux)

```bash
# 1. Entorno (Python 3.12). Siempre con --no-deps: ver docs/CONTRATO.md §10
python3.12 -m venv .venv
.venv/bin/pip install --no-deps -r requirements-dev.txt
.venv/bin/pip install --no-deps -e .
PLAYWRIGHT_BROWSERS_PATH=0 .venv/bin/playwright install chromium

# 2. MediaMTX (verificado por SHA-256) y vídeos de prueba
.venv/bin/python -m tools.fetch_mediamtx
.venv/bin/python -m tools.download_test_assets

# 3. Configuración local
cp .env.example .env

# 4. Pruebas
.venv/bin/python -m pytest                 # todas
.venv/bin/python -m pytest -m "not e2e"    # solo las rápidas
.venv/bin/python -m pytest tests/central   # panel central y despliegue

# 5. Todo el sistema con una sola orden (sin cámaras reales)
.venv/bin/python -m tools.dev_run start --seed --central   # Ctrl+C para parar
.venv/bin/python -m tools.dev_run start --seed --central --detach   # o en segundo plano...
.venv/bin/python -m tools.dev_run status
.venv/bin/python -m tools.dev_run stop                     # ...y se para así

# 6. Prueba de sistema de punta a punta (unos 15 min; resultados en tests/e2e/RESULTADOS.md)
.venv/bin/python -m tests.e2e.system_check
```

`tools.dev_run start` arranca, por este orden: PostgreSQL embebido (pgserver, con `--pg` o
`--central`), el simulador de cámaras (`--sim`/`--seed`: un NVR estilo Hikvision y otro estilo
Dahua con su API HTTP simulada, más una cámara de puerta con el vídeo de personas), el backend
(`python -m vms`, que arranca y vigila MediaMTX), la analítica (`python -m analytics`, si hay un
modelo en `models/`) y el panel central (`--central`). Con `--seed` da de alta los equipos por la
API y reparte las cámaras en los 4 muros. Todo queda en `.tmp/dev/` (datos, registros y un `.env`
de desarrollo con contraseñas aleatorias que se muestran al arrancar). `stop` para los procesos en
orden inverso y comprueba que no queda ningún MediaMTX huérfano.

Para usar solo el simulador, sin el resto: `python -m tools.camsim --hikvision hik1:4 --dahua dah1:4
--video 1=tests/assets/people-walking-h264.mp4`.

Las pruebas de despliegue usan PowerShell 7, PSScriptAnalyzer y shellcheck si los encuentran
(`VMS_TEST_PWSH`, `VMS_TEST_PSSA`, `VMS_TEST_SHELLCHECK`); si no, se saltan y lo dicen.

## Estructura del repositorio

| Carpeta | Contenido |
|---|---|
| `vms/core` | modelos, ajustes (`.env`), credenciales cifradas, registros sin contraseñas, interfaces |
| `vms/vendors` | Hikvision ISAPI, Dahua CGI/RPC2, ONVIF y descubrimiento en red |
| `vms/engine` | motor de vídeo (MediaMTX): configuración, supervisión, grabación y reproducción |
| `vms/api`, `vms/web` | backend web e interfaz (panel, muros, reproducción, zonas) |
| `vms/db` | esquema y migraciones de PostgreSQL (`python -m vms.db.migrate`) |
| `analytics` | analítica de tienda (detector, seguimiento, línea, zona, alertas, informe semanal) |
| `central` | panel central multi-sede, latido de las sedes (directo a PostgreSQL o agente HTTP) |
| `deploy/windows` | instalador y desinstalador PowerShell, servicios WinSW, kiosco de 4 monitores |
| `deploy/kiosk` | lanzador de los muros en modo kiosco (un navegador por monitor) |
| `deploy/linux` | instalador para Ubuntu/Debian y unidades systemd |
| `docs` | guías de instalación, alta de equipos, red, hardware, pruebas, problemas y RGPD |
| `tools` | simulador de cámaras, mocks de fabricantes, descarga de MediaMTX, locks |
| `tests` | pruebas por módulo y extremo a extremo |

## Servicios y puertos

| Servicio | Proceso | Puerto | Quién accede |
|---|---|---|---|
| Backend VMS | `python -m vms` | 8600/tcp | navegadores de la red de la tienda / VPN |
| MediaMTX (hijo del backend) | `bin/mediamtx` | 8189/udp+tcp (vídeo WebRTC); el resto solo 127.0.0.1 | navegadores |
| Analítica | `python -m analytics` | — (lee el RTSP local) | — |
| Latido HTTP | `python -m central.agent` | — (sale hacia la central) | — |
| Panel central | `python -m central` | 8700/tcp | personal de Covert por la VPN |
| PostgreSQL | servidor central | 5432/tcp | sedes por la VPN |

## Licencias de terceros

El producto solo usa componentes con licencias permisivas (MIT, BSD, Apache, PSF, MPL) y LGPL
con enlace dinámico. **No** incluye nada GPL/AGPL (ni Ultralytics YOLO, ni PyAV de PyPI, ni mpv,
ni SDK propietarios de fabricantes). La lista completa con los textos de licencia está en
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) (se regenera con
`python -m deploy.third_party_notices`). Principales piezas:

| Componente | Uso | Licencia |
|---|---|---|
| MediaMTX v1.21.1 | streaming, grabación, reproducción, WebRTC | MIT |
| FastAPI, Starlette, Uvicorn | backend web y panel central | MIT / BSD-3-Clause |
| Pydantic, pydantic-settings | modelos y ajustes | MIT |
| httpx, httpcore | cliente HTTP (fabricantes, latido) | BSD-3-Clause |
| onvif-zeep-async, zeep, lxml | ONVIF | MIT / MIT / BSD-3-Clause |
| psycopg 3, psycopg-pool | PostgreSQL | LGPL-3.0 (biblioteca sin modificar) |
| libpq (en psycopg-binary) | cliente PostgreSQL | PostgreSQL |
| argon2-cffi | contraseñas de usuarios | MIT |
| cryptography, keyring | almacén de credenciales | Apache-2.0 o BSD / MIT |
| RF-DETR (nano/small/medium/base) | detector de personas | Apache-2.0 (XL/2XL prohibidos: PML) |
| OpenVINO, ONNX Runtime | inferencia en CPU | Apache-2.0 / MIT |
| supervision | línea y zona | MIT |
| trackers | seguimiento ByteTrack | Apache-2.0 |
| opencv-python-headless | lectura RTSP | Apache-2.0 (+ FFmpeg LGPL-2.1 en Windows/Linux) |
| NumPy, SciPy, Matplotlib | cálculo (dependencias de la analítica) | BSD / BSD / PSF |
| psutil | estado del sistema | BSD-3-Clause |
| SDK del proveedor LLM (`anthropic`) | redacción del informe semanal (solo servidor central) | MIT |
| WinSW v2.12.0 | servicios de Windows | MIT |
| Python 3.12 embebible + pip | intérprete en Windows sin Python | PSF-2.0 / MIT |

## Seguridad y privacidad

- Las contraseñas de los equipos van en el almacén del sistema o en un archivo cifrado; nunca en
  la configuración ni en los registros (los registros ocultan usuario y contraseña de las URL RTSP).
- Los secretos de proceso van en `.env`, con permisos restringidos por el instalador.
- MediaMTX solo escucha en `127.0.0.1` salvo el puerto de vídeo WebRTC; el firewall se abre solo en
  red privada.
- Guía de red: [docs/RED.md](docs/RED.md). RGPD: [docs/RGPD-EIPD.md](docs/RGPD-EIPD.md).

## Documentación

| Documento | Para qué |
|---|---|
| [INSTALACION-WINDOWS.md](docs/INSTALACION-WINDOWS.md) | instalar en el PC de control, servicios, kiosco de 4 monitores |
| [INSTALACION-LINUX.md](docs/INSTALACION-LINUX.md) | mini PC de tienda con Ubuntu |
| [ALTA-EQUIPOS.md](docs/ALTA-EQUIPOS.md) | preparar Hikvision/Dahua/NVR: RTSP, ONVIF, subflujo H.264, usuario de solo lectura, VLC |
| [REQUISITOS-HARDWARE.md](docs/REQUISITOS-HARDWARE.md) | GPU de 4 salidas, cálculo de disco, mini PC de analítica |
| [RED.md](docs/RED.md) | VLAN de cámaras, firewall, VPN Headscale/Tailscale |
| [PANEL-CENTRAL.md](docs/PANEL-CENTRAL.md) | servidor central, tokens de sede, API |
| [CHECKLIST-PRUEBAS.md](docs/CHECKLIST-PRUEBAS.md) | pruebas con hardware real antes de entregar |
| [PROBLEMAS.md](docs/PROBLEMAS.md) | 401, 454, imagen gris, H.265, retraso… |
| [RGPD-EIPD.md](docs/RGPD-EIPD.md) | hoja de protección de datos para el cliente |
| [EMPAQUETADO.md](docs/EMPAQUETADO.md) | paquete offline, instalador .exe, PyInstaller |
| [TERCEROS.md](docs/TERCEROS.md) | licencias, atribuciones y binarios verificados |
| [CONTRATO.md](docs/CONTRATO.md) | interfaces técnicas entre módulos |
