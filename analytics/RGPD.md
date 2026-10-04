# Analítica de tienda: qué datos se tratan y cuáles no

Documento de apoyo para la **Evaluación de Impacto (EIPD)** del responsable del tratamiento (la cadena
de supermercados). Describe el módulo de analítica tal como está construido. Unmanned Studio / el
integrador actúan como encargados del tratamiento.

## 1. Finalidad

1. **Contar personas** que entran y salen por la puerta de la tienda (afluencia por minuto).
2. **Medir la ocupación de la cola de cajas** y avisar al personal por Telegram cuando la cola supera
   un umbral durante un tiempo, para abrir otra caja.
3. **Informe semanal** de afluencia y colas para el responsable de la tienda.

No se usa para vigilar, identificar ni evaluar a nadie, ni clientes ni trabajadores.

## 2. Qué se procesa (y dónde)

| Dato | Dónde | Cuánto tiempo |
|---|---|---|
| Imagen del subflujo de la cámara (baja resolución) | Memoria RAM del PC de la tienda | Lo que tarda en analizarse (≈ 0,1 s). Se descarta enseguida |
| Rectángulos «aquí hay una persona» con un número temporal de seguimiento | Memoria RAM | Unos segundos; se olvida en cuanto la persona sale de imagen |
| Conteos por minuto: entradas, salidas, ocupación media/máxima, segundos sobre el umbral | PostgreSQL (servidor central, por VPN) | Según la política de conservación del cliente |
| Alertas de cola: zona, hora de inicio y fin, pico de personas | PostgreSQL + mensaje de texto a Telegram | Según la política del cliente / del grupo de Telegram |
| Informe semanal: cifras agregadas y texto | PostgreSQL + archivos .md/.html | Según la política del cliente |

## 3. Qué NO se hace (garantías técnicas)

- **No se guardan imágenes ni vídeo de la analítica.** Ni en disco, ni en la base de datos, ni en
  registros, tampoco en modo de depuración. El código no contiene ninguna llamada que escriba imágenes
  (lo comprueba la prueba automática `test_no_frames_written_to_disk_by_analytics_code`) y la prueba de
  integración verifica que tras analizar vídeo real no queda ningún archivo de imagen o vídeo en la
  carpeta de datos. (La grabación 24/7 del VMS es un tratamiento distinto, con su propia base legal y
  señalización; la analítica no la usa ni la amplía.)
- **No hay reconocimiento facial** ni biométrico, ni se calculan rasgos (edad, sexo, ropa...). El
  detector solo distingue «persona» de «no persona».
- **No hay identificación ni seguimiento entre cámaras o entre días.** El número de seguimiento es
  local a una cámara, dura segundos y no se almacena.
- **No se mide a los trabajadores.** No hay zonas sobre puestos de trabajo ni métricas por persona;
  la zona de cola se dibuja sobre el pasillo de espera de clientes. Recomendación al instalar: no
  dibujar zonas que cubran el puesto del cajero.
- **No sale vídeo de la tienda.** El análisis se hace en el PC de la tienda; al servidor central solo
  viajan números.
- **Telegram recibe solo texto** («7 personas en la cola de cajas 1-3 desde las 13:40»), nunca imágenes.
- **El proveedor LLM del informe** (configurable; puede desactivarse con `VMS_LLM_PROVIDER=none`)
  recibe solo las cifras agregadas de la semana (totales, medias por hora, número de avisos). Ningún
  dato personal ni imagen.
- **El editor de zonas** muestra una captura de la cámara para dibujar; se sirve en memoria con
  `Cache-Control: no-store` y no se escribe en disco.
- **Sin envíos a terceros no previstos:** se bloquea la telemetría de uso que el paquete OpenVINO
  enviaría por defecto a Google Analytics al importarse.

## 4. Recomendaciones para el responsable

- Cámara de puerta **cenital** (mirando hacia abajo): cuenta mejor y no capta caras.
- Usar el **subflujo** de baja resolución para la analítica (es lo que hace el sistema por defecto).
- Informar en el cartel de videovigilancia de la finalidad de conteo de afluencia y gestión de colas.
- Restringir el grupo de Telegram al personal que debe actuar ante las colas.
- Definir el plazo de conservación de los conteos agregados (no son datos personales, pero conviene
  fijarlo en la política).

## 5. Componentes y licencias

Detector RF-DETR nano/small (Apache-2.0), seguimiento ByteTrack del paquete `trackers` (Apache-2.0),
geometría de línea/zona de `supervision` (MIT), OpenVINO (Apache-2.0) u ONNX Runtime (MIT), OpenCV
(Apache-2.0, con FFmpeg LGPL en Windows/Linux). Todo se ejecuta localmente.
