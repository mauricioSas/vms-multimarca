# Prueba de sistema de punta a punta — resultados

**Última ejecución:** 4 de octubre de 2026, 21:32–21:41 (hora de Madrid), **después de las
correcciones de la revisión de robustez y seguridad** → **los 10 pasos OK** (código de salida 0,
9 min 29 s). **Ejecución de referencia** (detalle por paso más abajo): 17:42–17:52, antes de la
revisión. **Equipo:** MacBook Pro Apple M1 Pro (8 núcleos, macOS, arm64) · **MediaMTX** v1.21.1 ·
**Python** 3.12 · **Chromium** de Playwright (sin ventana) · **PostgreSQL** embebido (pgserver) ·
**RF-DETR nano** exportado a OpenVINO.

Cómo repetirla:

```bash
.venv/bin/python -m tests.e2e.system_check          # unos 10 minutos
# opciones: --record-seconds 150  --load-seconds 300  --load-cams 16
```

El script arranca cada pieza como en una sede real, **cada una en su propio proceso**: simulador de
cámaras → `python -m vms` (que lanza y vigila MediaMTX) → `python -m analytics` → PostgreSQL →
`python -m central` y `python -m central.agent`. Chromium hace de los 4 monitores en modo kiosco.
Todo lo que sale aquí son **valores medidos en esa ejecución**. Los datos completos (JSON por paso y
muestras de CPU y memoria cada 10 s) están en [`RESULTADOS-datos.md`](RESULTADOS-datos.md), que
genera el propio script, y en `results-sistema.json`. Las capturas están en
[`screenshots/sistema/`](screenshots/sistema/).

## Resumen

| Paso | Qué se probó | Resultado (17:42 y 21:32) |
|---|---|---|
| a | Simulador: 2 cámaras estilo Hikvision + 2 estilo Dahua + 1 cámara con vídeo real de personas | **OK** |
| b | Arranque, login, alta por la API y 4 muros con layouts distintos | **OK** |
| c | Vídeo WebRTC en todas las celdas de `/wall/1` a `/wall/4` | **OK** |
| d | 173 s de grabación, línea de tiempo, MP4 válido con ffprobe y reproducción desde la interfaz | **OK** |
| e | Corte de 20 s de una cámara y recuperación automática de la vista y de la grabación | **OK** |
| f | MediaMTX matado con SIGKILL; el supervisor lo relanza | **OK** |
| g | Analítica real: conteos por minuto en PostgreSQL, alerta de cola por Telegram simulado e informe semanal | **OK** |
| h | Panel central: latido directo y por agente HTTP, sede visible | **OK** |
| i | Carga: 16 flujos en un muro 4x4 durante 5 minutos | **OK** (ver comentarios) |
| z | Parada ordenada, sin procesos huérfanos y sin contraseñas en disco ni en los registros | **OK** |

## Repetición tras la revisión de robustez y seguridad (21:32–21:41)

Qué cambió en el sistema antes de repetirla: MediaMTX ya no admite peticiones anónimas (usuarios
internos con contraseña), nombres de grabación con desfase horario (`%z`), cookie de kiosco firmada,
argon2 fuera del bucle, límite por IP, CSP, registro de auditoría, analítica con bucle compatible
con Windows, cola en disco con índice, migración `0002_site_row_security` (RLS) y formularios de
login que ya no se envían solos. Lista completa en [`docs/ESTADO.md`](../../docs/ESTADO.md).

**Primer intento (21:19–21:29): 8 pasos OK y 2 FALLO.**
- Paso d: la reproducción en la interfaz no cargó. El administrador pulsó «Entrar» antes de que cargara
  el script de la página de login. El navegador envió el formulario por GET, con la contraseña en la URL,
  y la sesión no llegó a abrirse. **Era un fallo real del producto**, no de la prueba. Ahora el botón
  llega desactivado hasta que el script está listo y el formulario es POST: hallazgo 23 de
  `docs/ESTADO.md`, con su prueba de regresión.
- Paso z: falló como consecuencia del paso d. El registro de auditoría no tenía ningún
  `recording_view` porque la reproducción en la interfaz no llegó a hacerse.

**Segundo intento (21:32–21:41): 10/10 OK.** Valores medidos (los completos están en
[`RESULTADOS-datos.md`](RESULTADOS-datos.md) y `results-sistema.json`):

| Paso | Referencia 17:42 | Tras la revisión 21:32 |
|---|---|---|
| b) Backend responde · cámaras en línea y grabando | 0,7 s · 1,2 s | 0,7 s · 1,2 s |
| c) 4 muros en vivo (todas las celdas) | 2,5–5,0 s; 15 sesiones WebRTC | 2,5 s en los 4; 15 sesiones WebRTC |
| c) API de MediaMTX sin credenciales | (no se comprobaba: respondía 200) | **401** |
| d) Línea de tiempo · clip MP4 30 s (ffprobe) · reproducción en la interfaz | 173 s · h264 640x360, 300 fotogramas · avanza 1,53 s | 165 s · h264 640x360, 300 fotogramas, 30,000 s · avanza 1,52 s |
| e) Corte de 20 s: API vuelve · celda vuelve (desde que se relanza) · hueco | 4,6 s · 10,8 s · 25,8 s | 4,6 s · 8,7 s · 25,8 s |
| f) MediaMTX con SIGKILL: relanzado · cámaras · muros | 1,3 s · 1,3 s · 21–29 s | 1,3 s · 1,3 s · 21–29 s |
| g) Analítica: fps procesados · inferencia p50/p95 | 7,96 · 118/138 ms | 8,0 · 102/126 ms |
| g) Conteos en PostgreSQL · alerta Telegram · informe | 4 min, 137/120 · 1 · ok | 3 min, 113/99 · 1 · ok (`template`) |
| h) Panel central: latido directo y agente HTTP | OK | OK |
| i) 16 flujos 5 min: imágenes perdidas · reconexiones | 0 · 0 (48 569 decodificadas) | 0 · 0 (48 581 decodificadas) |
| i) CPU media: backend · MediaMTX · navegador · analítica | 0,3 · 11,6 · 25,7 · 418 % | 0,3 · 12,2 · 26,2 · 464 % |
| i) Tendencia de RAM de MediaMTX | +1,56 MB/min | +1,36 MB/min |
| z) Huérfanos · secretos en disco o registros | ninguno · ninguno | ninguno · ninguno |
| z) Registro de auditoría | (no existía) | `recording_view` 1, `recording_download` 1, `live_main` 3 |

Las capturas de `screenshots/sistema/` son de esta última ejecución. El detalle por paso que sigue
describe la ejecución de referencia de las 17:42. En la última ejecución los valores son los de la
tabla anterior.

Además, en la batería `pytest` (459 pruebas, todas OK salvo 1 omitida que no aplica en macOS) hay pruebas de sistema más cortas con procesos reales
para lo que este script no recorre: **el muro en kiosco sigue con vídeo tras reiniciar el backend**
(`tests/e2e/test_kiosk_restart.py`, backend + MediaMTX + Chromium reales), **HTTPS con cookie
`Secure`** (`tests/api/test_tls.py`) y **RLS por tienda en PostgreSQL** (`tests/db/test_site_rls.py`).

## Detalle por paso

### a) Simulador de cámaras

- **NVR estilo Hikvision** `hik1` con 2 canales. Rutas nativas `/Streaming/Channels/101` y `102`, con
  Digest y la API ISAPI simulada en un puerto HTTP real.
- **NVR estilo Dahua** `dah1` con 2 canales. Rutas `/cam/realmonitor?channel=1&subtype=0/1`, con
  Digest y la API CGI simulada.
- **Cámara genérica** `door1` con el vídeo real de personas (`tests/assets/people-walking-h264.mp4`):
  principal a 1280x720 y subflujo a 640x360, ambos a 15 fps.
- La contraseña tiene caracteres conflictivos a propósito: `Sim#Pass:1@/x`.
- Resultado: los 10 flujos (5 principales y 5 subflujos) publicados en 1 s.

### b) Arranque, login, alta y muros

- El backend respondió en `/api/health` a los **0,7 s**. Un login con contraseña mala da **401**; el
  login de admin da 200.
- `POST /api/devices/test` del NVR Hikvision y del Dahua dio `ok: true` y `rtsp_ok: true`. El modelo
  se leyó por ISAPI/CGI: `DS-7608NI-K2/8P` y `DHI-NVR4208-8P-4KS2/L`.
- `POST /api/devices` con `import_channels: "all"` creó 2 cámaras por NVR, sin `import_error`. La
  cámara genérica se dio de alta con su ruta manual (`/ch1/main`, `/ch1/sub`).
- Muros: monitor 1 en **2x2** (las 4 cámaras Hik/Dahua), monitor 2 en **1x1** (puerta), monitor 3 en
  **3x3** (las 5 cámaras) y monitor 4 en **4x4** (5 cámaras y celdas vacías).
- Las 5 cámaras aparecieron **en línea y grabando a los 1,2 s**. `/api/status` dio `ok`.

### c) Vídeo WebRTC en los 4 muros (Playwright)

Se abrió cada muro como lo hace el kiosco: `/api/auth/kiosk?token=…&next=/wall/N`, las 4 pestañas a la
vez. En cada celda asignada se comprobó `videoWidth > 0`, que `currentTime` avance y que el estado sea
`live`.

| Muro | Celdas | Todas en vivo tras | videoWidth | currentTime avanzado en 2,5 s | RTCPeerConnection abiertas |
|---|---|---|---|---|---|
| 1 (2x2) | 4 | 5,0 s | 320 (subflujo) | 2,5 · 2,5 · 2,6 · 2,6 s | 4 |
| 2 (1x1) | 1 | 2,5 s | 1280 (principal, por ser 1 celda) | 2,46 s | 1 |
| 3 (3x3) | 5 | 2,5 s | 320 y 640 | 2,48 – 2,6 s | 5 |
| 4 (4x4) | 5 | 2,5 s | 320 y 640 | 2,4 – 2,5 s | 5 |

- MediaMTX tenía exactamente **15 sesiones WebRTC** (4+1+5+5): no hay conexiones fantasma.
- **Doble clic** en una celda del muro 1: pasa al flujo principal (`main`, videoWidth 640). Con
  Escape vuelve a la rejilla y las 4 celdas reproducen de nuevo en 5 s.
- Ningún error de JavaScript en las consolas.
- Capturas: `c-muro-1.png` … `c-muro-4.png`, `c-muro-1-ampliado.png`.

### d) Grabación, línea de tiempo y reproducción

- `/api/recordings/{id}/timeline`: un tramo continuo de **173 s** por cámara (172,1 s la de puerta),
  con fechas UTC terminadas en `Z`.
- Clip de 30 s descargado con `/video?format=mp4&download=1`:
  - HTTP 200, `video/mp4`, 3,46 MB;
  - `Content-Disposition: attachment; filename="Entrada_20261004-174300.mp4"`;
  - **ffprobe**: `h264`, 640x360, **300 fotogramas**, duración **30,000 s**.
- **Reproducción desde la interfaz**: el admin abrió `/playback`, eligió zoom de 1 h e hizo clic en la
  línea de tiempo. El vídeo grabado se reprodujo a 640x360 y su `currentTime` avanzó 1,53 s en
  ~1,5 s. Captura: `d-reproduccion.png`.

### e) Corte de 20 s de una cámara

Se mataron a la vez el flujo principal y el subflujo del canal 1 del NVR Hikvision (cámara «Entrada»),
se esperaron 20 s y se relanzaron.

| Medida | Valor |
|---|---|
| La API detecta el corte | 0,9 s (`/api/status` = `degraded`, «cámaras sin vídeo») |
| Estado de la celda del muro durante el corte | `reconnecting` (captura `e-muro-1-corte.png`) |
| La API vuelve a dar la cámara en línea y grabando | **4,6 s** después de relanzar |
| La celda del muro vuelve a reproducir | **10,8 s** después de relanzar |
| Hueco en la grabación | 25,8 s (20 s de corte + detección y reconexión) |
| Grabación tras la vuelta | nuevo tramo; 13 s grabados a los 8 s de volver |
| Las otras 3 celdas del muro | siguieron reproduciendo sin cortes |

### f) MediaMTX matado con SIGKILL

| Medida | Valor |
|---|---|
| El supervisor relanza MediaMTX | **1,3 s** (pid 20619 → 20822, `restarts` = 1) |
| Las 5 cámaras vuelven en línea y grabando | 1,3 s después de matarlo |
| Muros en vivo otra vez (medido muro a muro, en serie) | muro 1 a los 21,3 s · 2 a los 23,8 s · 3 a los 26,3 s · 4 a los 28,8 s |
| El proceso antiguo | no queda vivo |

Los muros tardan unos 20 s más que el motor. El motivo es la espera creciente del cliente WHEP
(1, 2, 5, 10 s…) tras varios intentos fallidos mientras MediaMTX no estaba. Es el comportamiento
previsto, pero se puede mejorar (ver «Mejoras propuestas»).

### g) Analítica, PostgreSQL, Telegram e informe semanal

Configuración de la cámara de puerta:
- analítica activada a 10 fps con `rfdetr-nano` y confianza 0,4;
- línea «Entrada» horizontal a media altura;
- zona «Cola cajas» en la mitad inferior, con umbral 3 personas, 3 s y cooldown de 60 s;
- Telegram activado en Ajustes, con la Bot API apuntando a un servidor simulado
  (`VMS_ANALYTICS_TELEGRAM_API_BASE`).

La analítica tomó la configuración del backend (`GET /api/internal/analytics/config`, token interno).

| Medida | Valor |
|---|---|
| Estado (`/api/analytics/status`) | `running`, no desactualizado. OpenVINO. Entrada a 15 fps, **procesado a 7,96 fps** (objetivo 10). Inferencia p50 **118,5 ms** y p95 138,4 ms |
| `line_counts_minute` | 4 minutos cerrados: 17/15, 41/36, 38/33 y 41/36 entradas/salidas (**137 entradas, 120 salidas**) |
| `zone_occupancy_minute` | 4 minutos, 1646 muestras, media 9,67 personas, máximo 13, 200 s por encima del umbral |
| `queue_alerts` | 1 alerta, avisada (`notified_at` relleno), pico 11 personas |
| Telegram simulado | 1 mensaje, token y chat correctos, **sin imagen**: «COLA EN CAJAS · Tienda E2E / Zona «Cola cajas» (Puerta principal): 8 personas en cola desde las 17:42 (aviso a partir de 3). Conviene abrir otra caja.» |
| `sites` / `site_cameras` / `analytics_rules` | nombre «Tienda E2E» (el que se puso en el panel), 5 cámaras, 2 reglas |
| RGPD | ningún archivo de imagen ni de vídeo en la carpeta de datos fuera de `recordings/` (las grabaciones normales del VMS) |
| Informe semanal | `python -m analytics.reports --site site-e2e-001 --week 2026-09-28 --no-llm`: código 0. Fila en `weekly_reports` con `status=ok` y `provider=template`. Se generaron `.md` y `.html`; copia en `informe-semanal-ejemplo.md` |

Los conteos son coherentes con el vídeo (una escena con mucha gente que cruza en ambos sentidos), pero
**no hay un conteo manual de referencia**, así que esto no mide la precisión.

### h) Panel central

- La sede apareció en `GET /api/sites` en el primer intento. El latido directo a PostgreSQL lo envía
  el backend cada 10 s.
- Datos de la sede: `online: true`, estado `ok`, nombre «Tienda E2E», 5/5 cámaras, analítica activa y
  hoy 137 entradas / 120 salidas / 1 alerta.
- Agente HTTP:
  - se emitió un token de sede (`POST /api/site-tokens/site-e2e-001`) y se creó un usuario operador
    en el backend;
  - `python -m central.agent --once` terminó con código 0 y `last_seen` avanzó;
  - el detalle de la sede trae 5 cámaras y 2 reglas.
- `GET /api/sites/{id}/counts?range=today&bucket=hour`: 137/120 en la franja de las 17:00 (hora de
  Madrid).
- Capturas: `h-central-sedes.png` y `h-central-sede.png`.

### i) Carga: 16 flujos en un muro 4x4 durante 5 minutos

Se añadió un tercer NVR simulado estilo Hikvision con 16 canales (640x360 principal y 320x180
subflujo, 10 fps), importado por la API. Su muro 4x4 quedó como única pestaña abierta.

En total el MediaMTX del sistema tenía **21 cámaras grabando** (16 + las 5 anteriores) y la analítica
seguía activa en la cámara de puerta. Se tomaron muestras cada 10 s durante 300 s. La CPU se expresa en
% de un núcleo (100 % = un núcleo entero).

| Proceso | CPU media | CPU máx. | RAM al inicio → al final | Tendencia de la RAM |
|---|---:|---:|---|---:|
| Backend (`python -m vms`) | 0,3 % | 0,5 % | 63,9 → 57,3 MB | −0,59 MB/min |
| MediaMTX | 11,6 % | 12,3 % | 111,9 → 136,9 MB (media 130,8 → 137,5) | +1,56 MB/min |
| Navegador (Chromium, todos sus procesos) | 25,7 % | 31,1 % | 703 → 629 MB | −1,76 MB/min |
| Analítica (1 cámara, nano a 10 fps) | **418 %** | 442 % | 440 → 435 MB | −0,54 MB/min |
| Simulador (MediaMTX + 42 ffmpeg, no es producto) | 66 % | 68 % | 982 → 947 MB | −2,48 MB/min |

- **Celdas**: 16/16 reproduciendo al empezar (en 5 s) y 16/16 al terminar. Se decodificaron 48 569
  imágenes, con **0 imágenes perdidas** y **0 reconexiones**.
- **Sesiones**: 16 RTCPeerConnection abiertas y 16 sesiones WebRTC en MediaMTX.
- **Equipo**: CPU total media del 81 % de los 8 núcleos (sobre todo la analítica y el simulador). RAM
  usada estable (5,99 → 5,93 GB).
- **¿Crece la memoria?**
  - Backend, navegador y analítica: no.
  - MediaMTX: subió de 112 a ~131 MB en el primer minuto (arranque de 16 lectores WebRTC y
    grabaciones) y después +1,6 MB/min (de 130,8 a 137,5 MB de media).
  - Con 5 minutos no se puede distinguir un llenado de búferes de una fuga lenta: hace falta la
    prueba de 24 h (ver «No probado»).
- Capturas: `i-muro-4x4-inicio.png` y `i-muro-4x4-final.png`.

### z) Parada

- Se pararon analítica, central y backend con SIGTERM. La analítica salió con código 0. Backend y
  central salieron con −15: uvicorn vuelve a lanzar la señal **después** de cerrar ordenadamente (en el
  registro: «Application shutdown complete» y «MediaMTX detenido»).
- **No quedó ningún MediaMTX huérfano.**
- Se revisaron todos los `.json`, `.log`, `.yml` y `.txt` de la carpeta de datos, de los registros y de
  la central. **No aparece** la contraseña de los equipos (ni en claro ni codificada con `%XX`), ni el
  token de Telegram, ni la contraseña del usuario del agente.

## Problemas encontrados por esta prueba (ya corregidos)

1. **El latido directo sobrescribía el nombre de la sede.**
   - El backend pasaba a `HeartbeatSender` una copia de la sede tomada al arrancar. Si se cambiaba el
     nombre o la zona horaria en el panel, el latido volvía a escribir el valor antiguo en `sites`
     cada 10-60 s. En la primera ejecución el informe salió como «Informe semanal · Sede».
   - Ahora el emisor recibe una función y la consulta en cada latido.
   - Prueba nueva: `tests/central/test_heartbeat.py::test_sender_reads_current_site_each_beat`.
2. **WHEP devolvía 500 justo después de dar de alta una cámara.** MediaMTX responde «path is not
   configured» mientras el motor aún no tiene la ruta. Ahora es un 404 `stream_not_available`, que el
   muro trata como «sin vídeo, reintenta».
3. **`VMS_MTX_WEBRTC_ICE_TCP=` no desactivaba ICE por TCP**, porque en el `.env` una variable vacía
   significa «valor por defecto». Ahora se escribe `off`.
4. Para poder probar Telegram sin Internet se añadió `VMS_ANALYTICS_TELEGRAM_API_BASE` (también sirve
   para un servidor Bot API propio o un proxy).
5. Plantilla del informe: decía «Se enviaron **1 avisos**» y «duraron de media 0 minutos» cuando la
   alerta seguía abierta. Ahora concuerda en singular y omite la duración si ninguna alerta ha
   terminado. Ojo: el ejemplo `screenshots/informe-semanal-ejemplo.md` se generó **antes** de este
   arreglo; la próxima ejecución de la prueba lo regenera.

Todo está anotado en `docs/CONTRATO.md` §12.

## Mejoras propuestas (no bloquean)

- **Reconexión de los muros tras caerse el motor (21-29 s).** El muro podría reiniciar la espera de
  reconexión de sus celdas cuando el evento SSE `status` diga que la cámara vuelve a estar en línea.
  Así bajaría a 2-3 s.
- **CPU de la analítica.** Con los hilos en automático, OpenVINO usó **4,2 núcleos** del M1 para una
  sola cámara de puerta a 10 fps, y aun así procesó 7,96 fps.
  - En un mini PC N150 (4 núcleos) eso no deja sitio para MediaMTX ni para más cámaras.
  - Antes del piloto, mide en el equipo real con `python -m analytics.tools.benchmark` y fija
    `VMS_ANALYTICS_INFERENCE_THREADS` (2-4) y los fps de la puerta (6-8) con datos.
- **Cobertura del informe.** Con pocos minutos de datos dice «la cámara de puerta tuvo datos el 0 % del
  tiempo». Es correcto para una prueba de minutos; en una semana real estará cerca del 100 %.

## No probado (y por qué)

| Qué | Motivo |
|---|---|
| Servicios de Windows (WinSW), `install.ps1`, `uninstall.ps1`, `install-kiosk.ps1`, firewall, `icacls`, tareas programadas y Python embebido | No hay Windows en este equipo. Solo se validaron con el analizador de PowerShell 7.6, PSScriptAnalyzer (sin avisos) y ejecutando sus funciones auxiliares en pwsh para macOS |
| Kiosco en 4 monitores físicos con Edge/Chrome, `--window-position`, escalado DPI y reapertura de ventanas | Hace falta el PC con 4 salidas. Aquí los 4 muros fueron 4 pestañas de Chromium sin ventana |
| Decodificación por hardware en el PC objetivo y CPU del navegador con 4 monitores de 16 celdas | Solo se midió 1 muro 4x4 en Chromium sin ventana en un M1 |
| Equipos Hikvision, Dahua u ONVIF reales (ISAPI/CGI/RTSP, H.265, NVR híbridos, bloqueo de usuario) | Sin hardware. Se usaron el simulador RTSP con rutas nativas y Digest, y las API HTTP simuladas. Lista de comprobación en `docs/CHECKLIST-PRUEBAS.md` |
| WS-Discovery por multicast en una red real y WebRTC entre equipos distintos o por la VPN | Todo corrió en el propio equipo (127.0.0.1) |
| Linux real (systemd, `PR_SET_PDEATHSIG`, almacén de credenciales en archivo sin escritorio) | Solo en macOS |
| Telegram real y proveedor LLM real | Sin token ni clave reales: Telegram simulado e informe con plantilla |
| Precisión del conteo frente a un conteo manual | Hace falta vídeo real de una puerta y de cajas de la tienda (fase 2 del plan) |
| Funcionamiento de 24 h o más (fugas lentas, recarga diaria del muro, disco lleno de verdad) | La carga duró 5 minutos. MediaMTX subió +1,6 MB/min en ese tramo: hay que confirmarlo con una prueba larga |
| Rendimiento en el mini PC N150/i5 | Todas las medidas son de un Apple M1 Pro |

Nota sobre la carga: durante toda la prueba seguían en marcha en este Mac un `mediamtx` y un `ffmpeg`
de una investigación anterior (lanzados a las 14:59, ajenos al proyecto). Ese ffmpeg consume ~15 % de
un núcleo; no se incluye en ninguna fila de la tabla, pero sí en la CPU total del equipo.
