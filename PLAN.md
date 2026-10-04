# Plan — VMS Multimarca + analítica (Unmanned Studio → Covert Security)

Estado: **fases 1-2 construidas; pendiente validación con hardware** · 4 de octubre de 2026

> El plan se aprobó y se construyó. Qué está terminado y probado, qué falta por probar con equipos
> reales y el siguiente paso: [`docs/ESTADO.md`](docs/ESTADO.md). Los cambios respecto a este plan
> están en la sección 8 (al final). El texto de las secciones 1-7 es el plan original.

## 1. Qué construimos

Un solo producto con dos módulos que comparten las mismas cámaras:

1. **VMS multimarca.** Muestra en vivo y graba cámaras y NVR **Hikvision + Dahua** mezclados, en **4 monitores** desde un PC, sin licencia por cámara.
2. **Analítica.** Cuenta las personas que entran por la puerta y vigila la cola de cajas. Avisa por Telegram, guarda los conteos en PostgreSQL y genera un informe semanal por tienda.

Se vende a **Covert**. El cliente final (Ametller) nunca nos compra directamente.

**En qué nos diferenciamos de HikCentral y DSS Pro** (los programas oficiales de cada marca):
- mezclamos marcas;
- incluimos la analítica de colas sin cambiar las cámaras;
- damos una vista central de muchas tiendas.

## 2. Decisión principal: no partimos de cero ni hacemos fork

Montamos el producto sobre piezas open-source maduras con licencia **MIT, BSD o Apache**. Así el producto puede ser cerrado y comercial. Nuestro código es la capa que une esas piezas y lo que ve el usuario.

| Pieza | Para qué | Licencia | Estado (oct-2026) |
|---|---|---|---|
| **MediaMTX** | Motor de cada sede. Hace una sola conexión por canal al NVR, graba 24/7 en fMP4 (como mucho se pierde 1 s si se va la luz) y borra lo antiguo de forma automática. Reproduce por API (`/list`, `/get`) y sirve WebRTC | MIT | v1.21.1, sep-2026 |
| **go2rtc** | Reserva para la vista en vivo si algún códec da guerra con WebRTC | MIT | activo |
| **onvif-zeep-async** | Hablar con cualquier cámara ONVIF | MIT | v4.3.0, sep-2026 |
| **hikvisionapi** | Listar canales y eventos de NVR Hikvision por ISAPI | MIT | activo |
| **rroller/dahua** (solo su `client.py`) | Lo mismo para Dahua: canales, eventos y PTZ | MIT | activo |
| **RF-DETR N/S** | Detectar personas, exportado a OpenVINO | Apache 2.0 | v1.11, oct-2026 |
| **supervision** + **trackers** | Línea de la puerta, zona de cajas y seguimiento (ByteTrack) | MIT / Apache | activo |
| **FastAPI + PostgreSQL** | Backend e informes | MIT / PostgreSQL | — |
| **Headscale** + cliente Tailscale | VPN para llegar a las 147 tiendas sin abrir puertos | BSD-3 | v0.29, sep-2026 |

**Prohibido por licencia:**
- Ultralytics YOLO (AGPL), incluidos sus modelos `.pt`.
- Los modelos XL/2XL de RF-DETR (licencia PML).
- python-amcrest (GPL).
- Shinobi (de pago).
- Las wheels de PyAV de PyPI (llevan FFmpeg GPL).
- Las DLL de mpv de terceros.
- Los SDK oficiales de Hikvision y Dahua, salvo que nos autoricen por escrito a redistribuirlos.

ZoneMinder, Bluecherry, Moonfire y Frigate se usan **solo para copiar ideas**, nunca su código. Frigate es MIT, pero no podemos usar su nombre.

## 3. Arquitectura

```
 Cámaras / NVR Hik + Dahua
          │  RTSP (1 conexión por canal)
          ▼
 ┌─────────────── PC de la sede (Windows o Linux) ───────────────┐
 │  MediaMTX ── graba 24/7 (fMP4) ── borra lo antiguo           │
 │     │  WebRTC / playback                                      │
 │  Servicio VMS (Python, FastAPI)                               │
 │     · alta de equipos (presets Hik/Dahua + ONVIF + escaneo)   │
 │     · genera la configuración de MediaMTX                     │
 │     · layouts de los 4 monitores · estado · usuarios          │
 │  Analítica (opcional por sede)                                │
 │     · RF-DETR (OpenVINO) + trackers + línea/zona              │
 │     · conteos por minuto → PostgreSQL · alertas Telegram      │
 └──────────────┬────────────────────────────────────────────────┘
                │ VPN Headscale (sale de la tienda, no abre puertos)
                ▼
      Central: inventario de sedes, salud, conteos, informe semanal
```

**4 monitores:** cada monitor tiene una ventana de Edge o Chrome en **modo kiosco** con WebRTC. Así el PC decodifica por hardware. La misma pantalla sirve después para entrar en remoto. Para elegir esto en vez de una aplicación de escritorio miramos dos cosas: consume menos CPU y nos evita los problemas de licencia de FFmpeg y mpv.

**Lo que ya está escrito y se reutiliza** (en `app/`): configuración con guardado atómico, contraseñas en el gestor de credenciales de Windows, presets RTSP de Hik y Dahua, logs que ocultan contraseñas y arranque con Windows.

**Lo que se descarta**, porque MediaMTX lo hace mejor: la grabación con ffmpeg, la retención y los segmentos propios. `live.py` (PyAV) también se descarta por el problema de licencia.

## 4. Fases

| Fase | Entregable | Duración estimada* | Requiere |
|---|---|---|---|
| **0. Validar con hardware** | Ver en VLC el RTSP de 1 Hikvision y 1 Dahua. Comprobar que el subflujo es H.264. Responder las preguntas de la sección 6 | 1 semana | Tu tío / un equipo de Covert |
| **1. VMS demo (Windows)** | Alta de equipos y escaneo de red, 4 monitores con layouts 1/4/9/16, doble clic para pantalla completa, grabación 24/7 con retención, reproducción con línea de tiempo, panel de estado y un instalador. **Es lo que tu tío enseña en Covert** | 2–3 semanas | PC con 4 salidas de vídeo para la prueba final |
| **2. Analítica piloto** | Conteo de puerta y de cola de cajas, Telegram, PostgreSQL y un primer informe semanal. Probado con el móvil (IP Webcam) y con vídeo grabado | 2–3 semanas | Vídeo real de una puerta y de cajas para calibrar |
| **3. Piloto 3 tiendas** | Mini PC por tienda (Linux), VPN, panel central y alertas de salud | 3–4 semanas | Visto bueno de Covert y Ametller, RGPD firmado |
| **4. Escalar** | Imagen de instalación fija, actualizaciones remotas, monitorización de 147 sedes | según el piloto | Contrato |

\* Son semanas de calendario. El código avanza rápido; lo que marca el ritmo es **probar con equipos reales**.

## 5. Cambios respecto al documento original

- **Puerta a 10–15 imágenes por segundo**, no a 1–2. Con menos de unas 5 fps, el seguimiento pierde a la gente que cruza. La zona de cajas sí puede ir a 1–2 fps. Por eso el mini PC tiene que ser un **N150 o un i5**, no un N100. Se confirma midiendo en la fase 2.
- **Cámara de puerta cenital** (mirando hacia abajo) siempre que se pueda. Cuenta mucho mejor y no capta caras, lo que también ayuda con el RGPD.
- **Subflujos en H.264**, porque los navegadores no reproducen bien H.265 por WebRTC. Se cambia en la configuración del NVR.
- **RGPD:** el análisis se hace dentro del PC de la tienda y la imagen se descarta al momento. Nosotros firmamos como encargados del tratamiento y Ametller hace su EIPD. Lo presentamos como un punto a favor.

## 6. Preguntas para tu tío / Covert (antes de la fase 1)

1. ¿Qué marcas y modelos de NVR hay en un sitio típico y cuántas cámaras tiene?
2. ¿Los NVR son accesibles en la red local con usuario y contraseña de administrador? ¿Hay VPN hacia las tiendas?
3. ¿Dónde estarían los 4 monitores: una central de Covert o cada tienda?
4. ¿Qué usan hoy para mezclar marcas y qué es lo que no les gusta?
5. ¿Se puede conseguir **un NVR o cámara de cada marca** prestado para probar?

## 7. Riesgos conocidos

- **Los eventos propios de cada marca** (alarmas, analítica del NVR) no viajan completos por ONVIF. Hay que integrarlos marca por marca vía ISAPI/CGI. En la fase 1 solo hacemos vídeo; los eventos quedan para después.
- **Rendimiento con 16 o más cámaras en un solo PC**: no está verificado. Se mide en la fase 1.
- **Nadie ha probado RF-DETR en un N150 con nuestro caso.** Se mide en la fase 2.
- **go2rtc depende de un único mantenedor.** Por eso es solo la reserva.

## 8. Qué cambió respecto al plan (al construirlo)

| Plan original | Cómo quedó | Por qué |
|---|---|---|
| Fase 1 (VMS demo) y fase 2 (analítica) | **Construidas y probadas con simuladores** (cámaras RTSP con rutas reales de Hik/Dahua, API ISAPI/CGI/ONVIF simuladas, PostgreSQL embebido, Chromium real). Falta la fase 0: validar con 1 Hikvision + 1 Dahua reales y un PC Windows con 4 monitores | Sin hardware en el entorno de desarrollo |
| Fase 3 (piloto) | **Adelantadas** las piezas de software: panel central, latido directo y por agente HTTP con token por tienda, instaladores de Windows (WinSW) y Linux (systemd), informe semanal programado | Para que el piloto solo dependa de la instalación |
| `hikvisionapi` y `rroller/dahua` (MIT) | ISAPI y CGI **implementados por nosotros** (sin código de terceros) | Solo hacían falta pocas llamadas; menos dependencias y sin atribuciones extra |
| go2rtc como reserva | No se ha necesitado | WebRTC de MediaMTX funcionó con H.264 en todas las pruebas |
| Reutilizar `app/` (configuración, credenciales, presets, logs) | Reescrito en `vms/core` tomando sus ideas; el código antiguo queda en `legacy/` solo como referencia | Faltaban tipos, pruebas y el modelo de datos nuevo |
| Contraseñas en el gestor de credenciales de Windows | En los servicios (cuenta SYSTEM, sin sesión de usuario) se usa el **almacén cifrado en archivo** (`secrets\`, solo SYSTEM/Administradores); el gestor del sistema sigue disponible en modo escritorio | El gestor de credenciales de Windows es por usuario y los servicios no tienen sesión |
| RF-DETR N/S en OpenVINO | Hecho (nano y small exportados). Se fuerza precisión **f32**; con f16 detectaba la mitad de personas en CPU ARM. Telemetría de OpenVINO bloqueada | Medido en las pruebas |
| MediaMTX solo en 127.0.0.1 | Además **con usuario y contraseña internos** (sin acceso anónimo) | Revisión de seguridad: cualquier programa del PC podía leer las contraseñas de los NVR por su API |
| Un rol de PostgreSQL para las tiendas | **Un rol por tienda** con Row Level Security | Revisión de seguridad: un PC comprometido podía tocar los datos de las 147 tiendas |
| Web por HTTP | HTTPS opcional para la LAN (`python -m vms tls-cert`); por la VPN ya va cifrado | Revisión de seguridad |
| — | Registro de auditoría de accesos a grabaciones (`logs/audit.log`) | RGPD art. 32 / guías de la AEPD |
| Headscale + Tailscale | Documentado (`docs/RED.md`), **no desplegado** | Se monta en la fase 3 con los equipos del piloto |

