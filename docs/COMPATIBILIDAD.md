# Compatibilidad por marca: madurez real de cada driver

Esta matriz dice **qué se ha comprobado de verdad** con cada marca. Es la que se publica: la v2 no se anuncia
como «15 marcas verificadas», sino con lo que diga esta página el día de la publicación. La madurez de cada
driver está en su archivo (`vms/vendors/drivers/<id>.py`), sale en el alta, en `GET /api/vendors` y en esta
tabla; `tests/vendors/test_maturity_matches_matrix.py` falla si no coinciden.

## Qué significa cada madurez

| Madurez | En la interfaz | Qué exige |
|---|---|---|
| `verified` | ✔ Verificado con hardware | una fila **superada** en «Pruebas de 72 h» (abajo) con ese driver: descubrir, alta, directo principal y subflujo, grabar 72 h con reconexión tras reinicio y cambio de IP, reproducir y captura (PLAN-V2 §4.7) |
| `fixtures` | ✔ Probado con respuestas reales | al menos una carpeta de fixtures **capturada de un equipo real** (`"synthetic": false` en su `meta.json`) que pasa la batería de contrato |
| `community` | ⓘ Según documentación pública | el driver sigue la documentación pública de la marca y pasa la batería con fixtures **sintéticas** (los simuladores del repositorio); no se ha probado con un equipo |
| `experimental` | ⚠ Experimental | como `community`, pero con puntos de la documentación que no se han podido contrastar (se dicen en sus avisos) |

Las fixtures sintéticas se generan con la misma herramienta que captura un equipo real
(`python -m tools.capture_device.synth`) y sirven para que cada driver tenga su batería, pero **no suben la
madurez**: solo una captura real (`python -m tools.capture_device --host … --driver …`) lo hace.

## Estado a 5 de octubre de 2026

**Resumen para publicar:** 0 verificados con hardware, 0 probados con respuestas reales, 13 según
documentación pública y 2 experimentales.

Todavía no hay laboratorio (decisión D6 pendiente): **nada de esta tabla se ha probado con un equipo
real**. Hikvision y Dahua tienen que llegar a `verified` (cámara, NVR y DVR/XVR) antes del piloto (PLAN-V2 §3.4).

## Matriz de drivers

| Driver | Marcas | Tipo | Capacidades en la v2 | Madurez | Fixtures |
|---|---|---|---|---|---|
| `hikvision` | Hikvision, HiLook, HiWatch, LTS, Annke | API ISAPI | modelo, serie y firmware; canales (NVR y DVR híbrido con ids del equipo); snapshot; «Corregir códec» con copia y deshacer; hora; ajustes de seguridad; WSD y SADP (solo búsqueda); bloqueo leído del `userCheck` | `community` | sintéticas: NVR 4 ch, cámara, DVR híbrido (IP desde el 33) |
| `dahua` | Dahua, Amcrest, Lorex | API CGI | modelo, serie y firmware; canales (NVR y XVR con canales IP detrás de los analógicos); snapshot con subflujo (`type=1`); «Corregir códec»; hora; ajustes de seguridad; WSD y DHIP (37810, LAN y 5 paquetes/s) | `community` | sintéticas: NVR 4 ch, XVR 4+4, cámara H.265 |
| `onvif` | cualquier marca con ONVIF | API ONVIF | GetDeviceInformation, GetServices, perfiles y URIs por **Media2 (Profile T)** o Media1, snapshot, hora con ajuste del desfase del reloj, ONVIF anónimo | `community` | sintéticas: Profile T con H.265, solo Media1 con un perfil |
| `generic` | — | RTSP manual | ruta escrita a mano | `community` | sintética (sin API ni ruta) |
| `ezviz` | Ezviz | perfil RTSP | `/ch1/main` y, si da 404, `/h264/ch1/main/av_stream` | `community` | sintética |
| `imou` | Imou | perfil RTSP + ONVIF | ruta de Dahua; contraseña = «safety code» (sin contrastar) | `experimental` | sintética |
| `uniview` | Uniview (UNV) | perfil RTSP + ONVIF | `/unicast/c{N}/s0/live` y `s1`; puerto 554 (a veces 9090) | `community` | sintética |
| `tplink-vigi` | TP-Link VIGI | perfil RTSP + ONVIF | `/stream1` y `/stream2`; aviso de Smart Coding | `community` | sintética |
| `tapo` | Tapo | perfil RTSP + ONVIF (2020) | `/stream1` y `/stream2`; cuenta de cámara; las de batería no tienen RTSP | `community` | sintética |
| `hanwha` | Hanwha Vision (Wisenet) | perfil RTSP + ONVIF | `/profile2/media.smp` y `profile3`; `/<canal-1>/…` en codificadores | `community` | sintética |
| `axis` | Axis | perfil RTSP + ONVIF | `/axis-media/media.amp?camera={N}&videocodec=h264` | `community` | sintética |
| `milesight` | Milesight | perfil RTSP + ONVIF | `/main` y `/sub`; si dan 404, `//main` y `//sub` | `community` | sintética (responde con doble barra) |
| `ajax` | Ajax | perfil RTSP 8554 + ONVIF | ruta copiada de la app o importada por ONVIF (sin ruta fija documentada) | `experimental` | sintética |
| `reolink` | Reolink | perfil RTSP | `/Preview_{NN}_main` y `_sub`; RTSP desactivado de fábrica | `community` | sintética |
| `bosch` | Bosch | perfil RTSP + ONVIF | `/?inst=1&line={N}` y `inst=2` | `community` | sintética |

Para la v2.1 quedan UniFi (RTSPS por Protect) y MJPEG por HTTP (vía go2rtc, MIT). Fuera de la v2: eventos,
PTZ, audio bidireccional y transcodificación.

## Pruebas de 72 h

| Driver | Equipo (modelo) | Firmware | Fecha | Resultado | Notas |
|---|---|---|---|---|---|

Ninguna todavía.

## Matriz por modelo y firmware (hardware real)

Se rellena en el laboratorio con `docs/CHECKLIST-PRUEBAS.md`. Columnas: marca, modelo, firmware, descubrir,
alta, directo principal, directo subflujo, grabar, reproducir, snapshot, 72 h, fecha.

| Marca | Modelo | Firmware | Descubrir | Alta | Principal | Subflujo | Grabar | Reproducir | Snapshot | 72 h | Fecha |
|---|---|---|---|---|---|---|---|---|---|---|---|

Sin filas: no hay equipos en el laboratorio todavía.

## Qué se ha comprobado sin hardware (y cómo)

- **Batería de contrato** (`pytest tests/vendors/contract`): para cada driver y cada carpeta de fixtures,
  `probe()`, canales, rutas con credenciales `%XX`, códec del SDP (también sin `rtpmap`, sin `sprop-*` y con
  saltos LF), un solo intento con una contraseña mala, snapshot en memoria, detección por las pistas del
  descubrimiento y que ningún registro contenga la contraseña.
- **Servidor RTSP caótico** (`tools/mocks/rtsp_chaos.py`): Digest SHA-256, varios retos, solo Basic, SDP raros,
  doble barra, 453/503, corte sin keepalive, bloqueo tras 3 fallos y primer fotograma a los 10 s.
- **Matriz de simuladores** (`pytest -m needs_mediamtx tests/compat`): MediaMTX + ffmpeg + mocks; los casos y
  su estado están en `tests/compat/LEEME.md`.
- **Lo que no se puede comprobar sin equipos** y queda marcado «no verificado» en el código: el formato exacto
  del bloqueo de Hikvision (`userCheck`) y de Dahua («Locked»), la numeración de canales de un DVR híbrido y de
  un XVR, el formato de respuesta de SADP y de DHIP, y la ruta y la contraseña de Imou y Ajax.
