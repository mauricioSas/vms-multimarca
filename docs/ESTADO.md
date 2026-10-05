# Estado del proyecto

## v2 · fase 0 (5 de octubre de 2026, rama `v2`)

Plan: [`PLAN-V2.md`](PLAN-V2.md) 1.2 · Contrato: [`CONTRATO.md`](CONTRATO.md) 2.0 (§13-§18) · Pruebas de
concepto: [`investigacion-v2/spikes.md`](investigacion-v2/spikes.md).

- **Hecho:** contrato v2.0; bloque B6 y decisiones de la noche del 5/10 en el plan (§9); esqueletos
  compartidos (modelos con `CONFIG_VERSION` 2 y migración, registro de drivers, eventos SSE, routers vacíos,
  permisos por cámara, `devices.js`, contenedores y módulos en las páginas, extensión del panel central);
  `native/` en Rust con `vms-common`; infraestructura de pruebas (prueba de la hora arreglada, ruff, mypy
  estricto en `vms/core`, cobertura, orden aleatorio, detector de voseo, vectores de redacción compartidos
  con Rust); CI en GitHub (`ci.yml` con un job por bloque y `s1-kit.yml`); S2 y S3 aprobadas; S4 en CI.
- **Pendiente del usuario:** ejecutar S1 en el PC del laboratorio (`spikes/s1-webview2/GUIA.md`).
  Decisiones con dinero o cuentas (D1, D3-D6, D12) sin cambios: la v2 avanza sin firma, sin YubiKey y sin
  Cloudflare (PLAN-V2 §9).
- **Siguiente:** arrancar B1, B3, B4, B5 y B6 en paralelo (B2 tras S1), cada uno con su job de CI.

---

## v1 (4 de octubre de 2026)

**Fecha:** 4 de octubre de 2026 · **Fase:** 1 (VMS) y 2 (analítica) construidas; piezas de software
de la fase 3 (panel central, latido, instaladores) también. **Pendiente: validación con hardware
real.**

Todo lo que se dice «probado» aquí se probó en un MacBook Pro M1 Pro (macOS) con simuladores, con
procesos reales y con un navegador real. Ningún equipo Hikvision o Dahua real ni ningún PC Windows
ha tocado este código todavía. Ese es el siguiente paso (al final).

## Cómo arrancarlo en desarrollo

```bash
.venv/bin/python -m tools.dev_run start --sim --seed --pg --central   # Ctrl+C para parar
```

Arranca PostgreSQL embebido, el simulador de cámaras (2 NVR estilo Hikvision/Dahua con sus API),
el backend con su MediaMTX, la analítica y el panel central en `.tmp/dev/`, y muestra las URL y las
contraseñas de desarrollo. Detalle en el [LEEME](../LEEME.md), sección «Inicio rápido para desarrollo».

## Resultados de las pruebas (última ejecución)

| Batería | Resultado |
|---|---|
| `pytest` completo (unidad, API, motor con MediaMTX real, analítica con RF-DETR real, PostgreSQL embebido, interfaz con Chromium, instaladores con PowerShell 7.6 + PSScriptAnalyzer y shellcheck) | **459 passed, 1 skipped** en 5 min 38 s. El omitido es la comprobación de FFmpeg LGPL de OpenCV, que no aplica en macOS (la wheel de macOS no se distribuye) |
| Prueba de sistema de punta a punta (`python -m tests.e2e.system_check`: simulador + 4 muros WebRTC + grabación/reproducción + corte de cámara + MediaMTX matado + analítica a PostgreSQL + Telegram simulado + informe + panel central + carga de 16 flujos) | **10/10 pasos OK** (21:32–21:41, 9 min 29 s). El primer intento dio 2 fallos por un problema real del login (hallazgo 23), ya corregido. Detalle en [`tests/e2e/RESULTADOS.md`](../tests/e2e/RESULTADOS.md) |
| Locks con hashes contra las plataformas de destino | `pip download --require-hashes --no-deps --only-binary=:all:` de `requirements-vms/analytics/central.txt` para **win_amd64** y **manylinux x86_64**: los 6 terminan con código 0 (225 MB y 240 MB de wheels verificadas) |

Cómo repetirlo:

```bash
.venv/bin/python -m pytest -q                     # unos 6 min
.venv/bin/python -m tests.e2e.system_check        # unos 10 min
# Para que no se omitan las pruebas de los .ps1 y .sh (herramientas portables, sin instalar nada):
VMS_TEST_PWSH=<ruta a pwsh> VMS_TEST_PSSA=<ruta a PSScriptAnalyzer.psd1> VMS_TEST_SHELLCHECK=<ruta a shellcheck> \
  .venv/bin/python -m pytest -q tests/central/test_deploy.py
```

## Terminado y probado (con simuladores)

| Pieza | Qué está probado |
|---|---|
| Alta de equipos | Prueba de conexión y lectura de modelo por ISAPI (Hikvision) y CGI (Dahua), RTSP con Digest, importación de canales, descubrimiento ONVIF (solo hacia destinos concretos), contraseñas con `@ # : / ?`, una contraseña rechazada no se reintenta (no bloquea el usuario del equipo) |
| Motor (MediaMTX) | Generación de configuración, rutas por API, grabación fMP4 24/7, retención y protección de disco, relanzamiento si muere (1,3 s), sin procesos huérfanos, pausa de 30 min solo con un 401 real, API interna con usuario y contraseña |
| Muros (4 monitores) | 4 muros 1x1/2x2/3x3/4x4 por WebRTC en Chromium, doble clic a flujo principal, «Sin señal» y reconexión, 16 flujos 5 min sin imágenes perdidas, **el muro en kiosco sigue con vídeo tras reiniciar el backend** |
| Grabación y reproducción | Línea de tiempo, reproducción en la interfaz, descarga MP4 válida (ffprobe), nombres de archivo sin ambigüedad en el cambio de hora (probado con MediaMTX real), registro de auditoría |
| Analítica | RF-DETR nano en OpenVINO sobre vídeo real de personas, línea de puerta, zona de cola, alerta por Telegram simulado, conteos por minuto en PostgreSQL, cola en disco si PostgreSQL cae, disco lleno, reloj que retrocede |
| Informe semanal | Cálculo de métricas en SQL y redacción con plantilla (proveedor LLM configurable; probado con `--no-llm`) |
| Panel central | Sedes, en línea/caída, conteos, comparativa, colas, informes, usuarios; latido directo y por agente HTTP con token por tienda; límite de intentos que no se esquiva con `X-Forwarded-For` |
| Seguridad | Sesiones, roles, CSRF, CSP, límite de intentos por IP y por usuario, contraseñas cifradas en reposo, sin secretos en registros, HTTPS opcional, rol de PostgreSQL por tienda con Row Level Security |
| Licencias | Sin AGPL/GPL en el producto, solo pesos RF-DETR nano/small (Apache-2.0), `THIRD_PARTY_NOTICES.txt` al día, locks con SHA-256 |

## Hecho pero NO probado (y cómo probarlo con hardware real)

La lista completa para el día de la prueba está en [`docs/CHECKLIST-PRUEBAS.md`](CHECKLIST-PRUEBAS.md)
(secciones A-F2). Lo más importante:

| Qué | Por qué no se probó | Cómo probarlo |
|---|---|---|
| Instalador de Windows (`install.ps1`, servicios WinSW, firewall, `icacls`, Python embebido, tarea del informe) | No hay Windows en el equipo de desarrollo. Solo se validó con el analizador de PowerShell 7.6, PSScriptAnalyzer y ejecutando sus funciones auxiliares | Checklist A: instalar en Windows 10 y 11, reiniciar el PC, matar procesos |
| Bucle de eventos en Windows (psycopg) | Se emuló Windows en macOS (`sys.platform` y la clase Proactor), que es exactamente lo que comprueba psycopg | Checklist F2: mini PC Windows con analítica → `db.ok = true` |
| Permisos de la carpeta de datos en Windows (grabaciones, usuarios, registros solo para SYSTEM/Administradores) | Sin Windows | Checklist F2: abrir esas carpetas con un usuario normal → Acceso denegado |
| Kiosco en 4 monitores físicos (posición de ventanas, DPI, decodificación por hardware) | Sin PC de 4 salidas | Checklist C |
| Equipos Hikvision, Dahua u ONVIF reales (H.265, NVR híbridos, firmwares) | Sin hardware | Checklist B con 1 Hikvision + 1 Dahua |
| HTTPS desde otro PC de la LAN con el certificado importado | Solo se probó en el propio Mac | Checklist F2 y `docs/RED.md` |
| Linux real (systemd, mini PC N150/i5) | Solo macOS | `docs/INSTALACION-LINUX.md` en el mini PC |
| Precisión del conteo frente a un conteo manual | Hace falta vídeo real de la puerta y de cajas | Checklist E (fase 2 del plan) |
| Funcionamiento de 24 h o más | La carga duró 5 minutos | Checklist C y D |
| Telegram y proveedor LLM reales | Sin token ni clave reales | Poner `VMS_TELEGRAM_BOT_TOKEN` y `VMS_LLM_API_KEY` y generar un informe |

## Revisión de robustez y seguridad: hallazgos

Dos revisores (robustez y seguridad) encontraron 22 problemas (y la prueba de sistema, uno más), cada uno con un script o una lectura
de código que lo reproducía. Antes de corregirlos se confirmó que el código seguía teniendo el fallo,
y cada corrección tiene su prueba de regresión, que recrea la condición del fallo: 74 pruebas en total (`tests/*/test_review_*.py`, `tests/core/test_aio_windows.py`,
`tests/e2e/test_kiosk_restart.py`, `tests/db/test_site_rls.py`, `tests/api/test_tls.py`, 7 en
`tests/central/test_deploy.py` y 4 en `tests/web/test_static_assets.py`). **No se descartó ninguno**: todos eran reales.

| # | Gravedad | Hallazgo | Estado | Prueba |
|---|---|---|---|---|
| 1 | Crítico | Windows: psycopg asíncrono no funciona con el bucle Proactor (conteos que no llegan, panel central e informe caídos) | **Corregido** (`vms.core.aio`: bucle selector en analítica, informe y central; latido del backend con psycopg síncrono en un hilo). Falta confirmarlo en Windows | `tests/core/test_aio_windows.py` (reproduce el fallo con Proactor y lo resuelve) |
| 2 | Crítico | API de MediaMTX sin autenticación: cualquier proceso del PC leía las contraseñas de los NVR y ejecutaba comandos | **Corregido**: sin usuario anónimo; usuarios internos con hash. **Pendiente** (defensa extra): que los servicios de Windows corran con una cuenta virtual sin privilegios en vez de SYSTEM; no se cambió porque no se puede probar sin Windows | `tests/engine/test_review_engine.py` (MediaMTX real: 401 sin credenciales y `runOnInit` rechazado) y paso c de la prueba de sistema |
| 3 | Alto | Tras reiniciar el backend, los muros en kiosco se quedaban en `/login` hasta 6 h | **Corregido**: cookie de kiosco firmada, válida tras un reinicio | `tests/api/test_review_security.py`, `tests/e2e/test_kiosk_restart.py` (backend real + Chromium) |
| 4 | Alto | Windows: grabaciones, usuarios, configuración y registros legibles por cualquier usuario local | **Corregido** en `install.ps1` (toda la carpeta de datos solo para SYSTEM y Administradores). Falta ejecutarlo en Windows | `tests/central/test_deploy.py::test_windows_data_dir_is_locked_down` (estática) |
| 5 | Medio | «401» dentro de un puerto (5401) pausaba el equipo 30 min | **Corregido** | `tests/engine/test_review_engine.py` |
| 6 | Medio | Cambio de hora de octubre: una hora de grabación se solapaba con la siguiente | **Corregido** (`%z` en el nombre; renombrado de los archivos antiguos) | MediaMTX real con nombres de la noche del cambio |
| 7 | Medio | Disco lleno: se perdían minutos cerrados y no salía el aviso de Telegram | **Corregido** | `tests/analytics/test_review_robustness.py` |
| 8 | Medio | Cola local con PostgreSQL caído varios días bloqueaba el bucle ~0,45 s/s | **Corregido** (índice en memoria) | ídem (43 200 lotes) |
| 9 | Medio | Cambiar la IP de un equipo y pulsar «Probar» revelaba su contraseña guardada | **Corregido** (422: hay que volver a escribir la contraseña) | `tests/api/test_review_security.py` |
| 10 | Medio | Login: argon2 bloqueaba el bucle y el límite se esquivaba cambiando de usuario | **Corregido** (hilos + límite por IP + memoria acotada) | ídem |
| 11 | Medio | Web solo por HTTP: login y contraseñas en claro por la LAN | **Corregido**: HTTPS opcional (`python -m vms tls-cert`); con HTTPS, el HTTP solo en 127.0.0.1. **Pendiente**: que el instalador lo active solo (hoy son 4 pasos a mano, `docs/RED.md`) | `tests/api/test_tls.py` (proceso real: HTTPS, cookie `Secure`, parada) |
| 12 | Medio | Panel central: el límite de intentos se esquivaba con `X-Forwarded-For` | **Corregido** | `tests/central/test_review_central.py` |
| 13 | Medio | Un solo rol PostgreSQL para todas las tiendas con acceso a las filas de todas | **Corregido**: migración `0002_site_row_security` + `python -m vms.db.site_roles` (un rol por tienda con Row Level Security) | `tests/db/test_site_rls.py` (PostgreSQL real) |
| 14 | Medio | Ver o descargar grabaciones no dejaba registro de acceso | **Corregido** (`logs/audit.log`) | `tests/api/test_review_security.py` y paso z de la prueba de sistema |
| 15 | Bajo | Reloj que retrocede: un minuto guardado se sobrescribía con un conteo parcial | **Corregido** | `tests/analytics/test_review_robustness.py` |
| 16 | Bajo | `clear_below >= umbral` aceptado: «cola normalizada» con la cola llena | **Corregido** (422 en la API y límite en la analítica) | ídem y `test_review_security.py` |
| 17 | Bajo | `secret.key` sin escritura atómica: un corte de luz podía dejar el backend sin arrancar | **Corregido** | `tests/core/test_review_credentials.py` |
| 18 | Bajo | Redirección abierta en el login del panel central | **Corregido** | `tests/central/test_review_central.py` |
| 19 | Bajo | Pesos RF-DETR sin su aviso Apache-2.0; el instalador copiaba los `.pth` (750 MB) | **Corregido** | `tests/central/test_deploy.py` |
| 20 | Bajo | Salvaguardas RGPD solo en la documentación (retención > 1 mes, zonas sobre el puesto de caja) | **Corregido** (aviso en API e interfaz; texto fijo en el editor de zonas) | `test_review_security.py` |
| 21 | Bajo | El backend VMS no enviaba Content-Security-Policy | **Corregido** | ídem |
| 22 | Bajo | Dependencias sin hashes | **Corregido** (SHA-256 de PyPI + `--require-hashes`) | `tests/central/test_deploy.py` y `pip download` para win_amd64 y manylinux |
| 23 | Medio | *(Encontrado al repetir la prueba de sistema.)* Si se pulsaba «Entrar» antes de que cargara el script de la página, el navegador enviaba el formulario por GET **con la contraseña en la URL** (historial del navegador) | **Corregido** en el login y el primer administrador del VMS y del panel central: botón desactivado hasta que el script está listo y formulario POST | `tests/web/test_static_assets.py::test_password_forms_never_submit_natively_with_get` y paso d de la prueba de sistema |

### Pendientes (no bloquean el piloto, pero hay que hacerlos)

1. **Servicios de Windows con cuenta virtual** (`NT SERVICE\VMSBackend`) en lugar de SYSTEM, dando a
   esa cuenta solo la carpeta de datos. Hoy, con la API de MediaMTX protegida y la carpeta de datos
   cerrada, un usuario local ya no llega a las contraseñas; esto es una capa más. Hay que probarlo en
   Windows (WinSW, permisos y el almacén de credenciales).
2. **HTTPS desde el instalador** (`install.ps1 -Tls`): generar el certificado, abrir 8643 en el
   firewall e importar el certificado en el propio PC.
3. **Prueba RTSP con Basic:** la prueba de conexión responde a un reto Basic por RTSP (algunos equipos
   antiguos lo piden). Va dentro de la VLAN de cámaras; si se quiere cerrar del todo, añadir una
   opción por equipo «permitir Basic».
4. Mejoras de la prueba de sistema anterior: reconexión de los muros más rápida tras caerse el motor
   (hoy 21-29 s) y fijar los hilos de OpenVINO en el mini PC real.

## Siguiente paso recomendado

**Prueba con 1 Hikvision + 1 Dahua reales y un PC Windows con 4 monitores** (fase 0 + cierre de la
fase 1 del plan), siguiendo [`docs/CHECKLIST-PRUEBAS.md`](CHECKLIST-PRUEBAS.md):

1. Instalar en Windows 11 con `install.ps1` (secciones A y F2 del checklist).
2. Dar de alta un NVR Hikvision y uno Dahua con al menos 2 cámaras cada uno; comprobar que el
   subflujo está en H.264 (sección B).
3. Abrir los 4 muros con `install-kiosk.ps1` y dejarlo 24 h (secciones C y D), incluido un
   `Restart-Service VMSBackend` con los muros abiertos.
4. Grabar 10 minutos de vídeo real de una puerta y de una cola de cajas para calibrar la analítica
   y medir su precisión con un conteo manual (sección E).
5. Con lo que salga, cerrar los pendientes de arriba y pasar al piloto de 3 tiendas.

## Primera instalación en Windows real (4 de octubre de 2026)

En Windows 11 (10.0.26200), con `-Components Backend,Analytics -PythonMode Embedded`, la instalación terminó y los servicios arrancaron: el backend respondió HTTP 200 y OpenVINO cargó RF-DETR nano y small. Por el camino salieron tres fallos de Windows PowerShell 5.1, ya corregidos y con su test:

1. **Python del sistema.** Si `py.exe` existe pero no hay Python 3.12, el texto que escribe en stderr detenía el script por `ErrorActionPreference=Stop`. Ahora se consulta con `Invoke-Probe`.
2. **pip en el Python embebible.** pip se niega a modificarse si se invoca como `pip.whl/pip`. Ahora la wheel se descomprime directamente en site-packages.
3. **Comillas en `python -c`.** PS 5.1 elimina las comillas dobles de los argumentos. Los `python -c` ya no las usan.
4. **Tildes rotas.** Los .ps1 se guardan ahora en UTF-8 con BOM.
