# Informe: funciones, IA de verificación, auditoría de seguridad y onboarding

> Informe de investigación para la v2 (5 de octubre de 2026). Las decisiones finales van en
> [`../PLAN-V2.md`](../PLAN-V2.md). Licencias, estrellas y actividad verificadas ese día con la
> API de GitHub, PyPI y npm. Los datos de CVE se consultaron en NVD y en el catálogo KEV de CISA
> (versión 2026.10.04).

**Cliente:** Covert Security, integrador de Barcelona, con una cadena de 147 supermercados.
**Restricciones:**
- Licencias solo MIT, BSD, Apache-2.0, ISC, zlib o MPL-2.0.
- Nada GPL ni AGPL. Nada de Ultralytics.
- RGPD estricto: ni reconocimiento facial, ni identificación de personas, ni nada que mida a
  trabajadores.

---

## 0. Resumen y prioridades

### Punto de partida (lo que ya tiene el producto)

**Ya existe:**
- Alta por ISAPI, CGI y ONVIF. Ya se leen el modelo y la versión de firmware (`DeviceInfo.firmware`).
- Muros 1–16 en 4 monitores, grabación 24/7 con MediaMTX y reproducción con línea de tiempo del día.
- Descarga MP4 y registro de auditoría de accesos.
- Analítica de puerta y colas con alertas de Telegram.
- Informe semanal con LLM opcional.
- Panel central con latido y roles.

**Falta:**
- PTZ, eventos del NVR, marcadores y exportación con cadena de custodia.
- Comprobación de la imagen de las cámaras.
- Informes de salud, permisos por cámara y onboarding.

### Lista priorizada para la v2

Esfuerzo: **S** ≤ 2 días · **M** de 3 a 7 días · **L** más de 1,5 semanas (de una persona, con
pruebas). Valor pensado para un integrador que mantiene 147 tiendas.

| # | Función | Valor | Esfuerzo | Por qué entra |
|---|---|---|---|---|
| 1 | **Verificación de imagen / sabotaje** (tapada, desenfocada, movida o girada, negra, congelada, IR, suciedad, contraluz, color) con **puntuación de salud de 0 a 100** | Muy alto | M | Genetec lo vende como módulo aparte (KiwiVision Camera Integrity Monitor). Hikvision lo tiene solo en algunas cámaras y por marca. Con OpenCV clásico, sin modelo nuevo, cuesta unos 10 ms por cámara y comprobación (§3.2) |
| 2 | **Informe de salud** por tienda y en el panel central: disponibilidad, huecos de grabación, días de retención reales frente al objetivo, disco, desfase horario y puntuación de imagen | Muy alto | M | Es lo que Covert factura como mantenimiento. HikCentral, Milestone y Nx lo traen. Con 147 tiendas no se puede revisar a mano |
| 3 | **Hora:** sincronización (NTP) y **detección de desfase** cámara↔PC | Alto | S | Una hora mal puesta invalida la prueba. En IPCamTalk y en IPVM hay muchos hilos de Hikvision con deriva horaria. Se lee por ONVIF, ISAPI y CGI |
| 4 | **Previsión de días de grabación** y aviso si no se llega al objetivo (o si se pasa de 30 días) | Alto | S | Muy poco código: tasa de bits real de MediaMTX × disco libre |
| 5 | **Exportación de evidencias:** SHA-256 por archivo, manifiesto firmado (Ed25519), acta de cadena de custodia, visor HTML portable y marca de agua en el visor | Muy alto | M | Es lo que pide la policía (sección §2.1). Encaja con el art. 22.3 de la LOPDGDD: entregar a la autoridad en 72 h |
| 6 | **Marcadores con bloqueo de evidencia** (el tramo no lo borra la retención) | Alto | S–M | Es el caso legal: conservar más de un mes **solo** lo que acredita un hecho |
| 7 | **Notificaciones**: correo y webhook, con reglas por tipo y por tienda y agrupación para no saturar (Telegram ya existe) | Alto | S | Con la biblioteca estándar basta; Apprise (BSD-2) es la opción si se quieren 100 servicios |
| 8 | **Exportación CSV** de conteos (tienda, día y hora) | Alto | S | Lo pide cualquier responsable de retail para Excel |
| 9 | **Asistente «¿por qué no conecta?»** con reglas, y un LLM opcional solo para redactar la explicación | Alto | S–M | Las pruebas ya existen por separado (`/test`, RTSP, 401). Hay que encadenarlas y explicarlas |
| 10 | **Onboarding**: asistente de primer uso, estados vacíos, ayuda «?» y recorridos con **Driver.js (MIT)** | Alto | M | Usuarios de tienda no técnicos y 147 instalaciones. Frigate 0.17 añadió un asistente parecido por la misma razón |
| 11 | **Auditoría de seguridad de equipos**: credenciales guardadas, servicios inseguros, RTSP u ONVIF anónimos, firmware con CVE (+ KEV) y hora | Muy alto (comercial) | M | Covert lo puede vender como servicio. Los datos de NVD y KEV son de uso libre (§4) |
| 12 | **Permisos por cámara** (rol con cámaras asignadas) | Alto | M | Principio de mínimo acceso (RGPD art. 32). Frigate 0.17 lo añadió por demanda |
| 13 | **Línea de tiempo con eventos**: huecos, sabotaje, analítica, marcadores y eventos del NVR | Alto | M | Se amplía la que ya hay en `playback.html` |
| 14 | **Eventos del NVR**: `alertStream` ISAPI, `eventManager` Dahua y E/S de alarma; recibe también el sabotaje que detecta la propia cámara | Medio-alto | M–L | El plan ya avisaba de que hay que hacerlo marca por marca. Además sirve de índice para la búsqueda inteligente |
| 15 | **Secuencia o rotación** en los muros | Medio | S | Lo traen Nx («Showreel»), Milestone («Carousel») y HikCentral |
| 16 | **Búsqueda por movimiento en una zona** | Medio-alto | L | Se hace bien solo con un índice (eventos de movimiento de la cámara, el n.º 14) más decodificación bajo demanda, como Frigate 0.18 |
| 17 | **Anonimizar la exportación** (difuminar a terceros) para el derecho de acceso | Medio-alto | M–L | Reutiliza RF-DETR: se difumina la caja de cada persona, sin identificar a nadie |
| 18 | **PTZ y presets** | Bajo-medio en supermercado | M | Pocos domos PTZ en tienda. Preguntar a Covert antes de hacerlo |
| 19 | Mapas o planos con cámaras (Leaflet, BSD-2) | Medio | M | Útil en la central; en tienda, poco |
| 20 | Búsqueda en lenguaje natural **sobre metadatos** y resumen de incidencias | Medio | S–M | El proveedor LLM ya existe; solo con agregados, nunca con imágenes |
| 21 | Conteo de vehículos (aparcamiento) | Bajo-medio | S | Solo en tiendas con aparcamiento; nunca matrículas |

**Propuesta de corte para la v2:** del 1 al 13. Del 14 al 17, en la v2.1. El 18 y el 19, solo si
Covert los pide. El 20 y el 21 son opcionales.

---

## 1. Método y fuentes

- **Productos comerciales:** documentación oficial de Milestone XProtect (exportación firmada y
  Smart Client Player), Nx Witness (Smart Motion Search y marca de agua con el usuario), Hikvision
  (Video Quality Diagnosis e ISAPI), Dahua DSS (VQDS), Genetec (KiwiVision Camera Integrity
  Monitor) y Blue Iris (watchdog).
- **Open source:** notas de las versiones 0.17 y 0.18 de Frigate (septiembre de 2026) y discusiones
  #3012, #7090 y #7443 sobre detectar cámaras tapadas.
- **Foros:** IPCamTalk (watchdog de Blue Iris, deriva horaria de Hikvision, API de Dahua), use-ip.co.uk
  e IPVM.
- **Reddit:** no se pudo leer (el buscador bloquea ese dominio). Lo que dicen los integradores se
  sacó de IPCamTalk, IPVM y GitHub.
- **Normativa y policía:** UK Police Requirements for CCTV v3.0, Recovery and Acquisition of Video
  Evidence v3.0 y LOPDGDD (arts. 22 y 89).
- **Datos de CVE:** API de NVD 2.0 (configuraciones CPE de CVE-2021-36260 y CVE-2021-33044), KEV de
  CISA descargado y filtrado, y EPSS de FIRST.
- **Prototipo propio** de detección de sabotaje con OpenCV 5.0 (la versión del producto) sobre
  `.tmp/e2e-system/clip.mp4` (§3.2). Es código desechable del scratchpad; **no se ha tocado el
  producto**.

---

## 2. Funciones que piden los integradores y los operadores

### 2.1 Exportación de evidencias y cadena de custodia

**Qué hacen los demás:**
- **Milestone:** el servidor de grabación firma lo grabado y el Smart Client vuelve a firmar al
  exportar (SHA-2). La exportación puede ir cifrada con AES-256. El reproductor portable (Smart
  Client – Player) tiene un botón «Verificar firmas».
- **Nx Witness:** marca de agua con el **nombre del usuario que exporta**, exportación solo de
  movimiento y exportación multicámara que se puede reproducir.
- **Frigate 0.18:** organiza las exportaciones en **casos** (incidentes).

**Qué pide la policía (guía del Reino Unido, la referencia más citada en Europa):**
- Exportar **en formato nativo** e incluir un reproductor gratuito que funcione **sin
  instalarse**.
- Un SHA-256 por archivo y un registro de continuidad con fecha.

En España, el art. 22.3 de la LOPDGDD obliga a poner la grabación a disposición de la autoridad en
**72 horas** desde que se conoce el hecho. Es la única excepción al borrado en un mes.

**Propuesta para nosotros (M):** un ZIP o una carpeta por caso con este contenido:

1. `video/`: los segmentos fMP4 **originales** de MediaMTX (son el «nativo») y un MP4 unido por
   cámara (remux sin recodificar).
2. `manifiesto.json`: con estos datos:
   - cámara, tienda, intervalo UTC y local, y desfase horario medido en ese momento (§2.9);
   - usuario y motivo (obligatorio), y número de caso o atestado;
   - SHA-256 de cada archivo.
3. `manifiesto.sig`: firma **Ed25519** del manifiesto, hecha con la clave de la instalación
   (`cryptography`, Apache/BSD, que ya está en los locks). La clave pública va en el ZIP y en el
   panel central.
4. `acta.html` o PDF: acta de cadena de custodia, con quién la exportó y quién la recibe.
5. `visor.html`: página local con `<video>`, lista de clips y **marca de agua superpuesta** (usuario
   y fecha). Comprueba los SHA-256 en el navegador (`crypto.subtle.digest`) y avisa si algo no
   coincide. No hay que instalar nada.
6. Una línea en `audit.log` (ya existe).

**Cuidado con estas trampas:**
- **No quemar la marca de agua en el original.** Recodificar rompe el «nativo» y el hash. Si se
  quiere una copia de trabajo con la marca de agua impresa, que sea un **segundo** archivo.
- **No uses libx264 para recodificar.** Es GPL y no cabe en un FFmpeg LGPL. Usa OpenH264 (BSD, el
  binario de Cisco), Intel QSV o NVENC, o haz la marca de agua solo en el visor.
- **H.265 en el visor:** Chrome y Edge lo decodifican en Windows con aceleración por hardware,
  pero no en todos los PC. El acta debe decir el códec y recomendar VLC como alternativa.
- **C2PA** (c2pa-rs, MIT/Apache, activo): interesante, pero hoy ningún perito policial lo comprueba.
  Queda para más adelante.

### 2.2 Marcadores y bloqueo de evidencia (S–M)

- Un marcador es un punto o tramo con nota en la línea de tiempo.
- **«Proteger»** copia los segmentos a `evidencias/` y los excluye de la retención. Equivale al
  «Evidence lock» de Milestone.
- Cada protección pide un motivo y una caducidad (por defecto 90 días, ajustable) y se ve en el
  informe de salud («3 tramos protegidos, el más antiguo de hace 40 días»).
- Así se cumple el art. 22.3: se guarda más tiempo **solo** lo justificado.

### 2.3 Búsqueda por movimiento en una zona (smart search) (L)

- **Nx:** dibujas un rectángulo y la línea de tiempo se pinta de rojo donde hubo movimiento. Usa
  metadatos de movimiento guardados al grabar.
- **Frigate 0.18** (septiembre de 2026): «Motion Search» analiza **bajo demanda**, pero se salta
  los segmentos sin movimiento gracias a mapas de calor guardados. Según su documentación, eso es lo
  que hace posible buscar en rangos largos.

**En nuestra arquitectura:**
- MediaMTX graba sin decodificar, así que no hay metadatos de movimiento.
- Decodificar el flujo principal de 16 cámaras de forma continua en un N150 es caro.

**Propuesta:**
1. **Índice barato:** los eventos de movimiento (VMD) que la cámara o el NVR ya generan, por
   `alertStream` (ISAPI) o `eventManager.cgi?action=attach` (Dahua) (función n.º 14). Se guardan
   en PostgreSQL y se pintan en la línea de tiempo.
2. **Búsqueda en zona:** se decodifican solo los segmentos marcados, a 1–2 fps y a 320 px, con
   OpenCV (FFmpeg LGPL, ya en uso), por diferencia de fotogramas dentro del polígono.
3. Si la cámara no envía eventos, se decodifica bajo demanda con un límite de rango (por ejemplo,
   4 h) y una barra de progreso.

Va después de los eventos del NVR.

### 2.4 Línea de tiempo con eventos (M)

- Se amplía la línea de tiempo propia de `playback.html` con estas capas: grabación o hueco,
  marcadores, sabotaje y salud, alertas de analítica (colas) y eventos del NVR.
- La propia es más ligera y ya está probada. Si se queda corta, la alternativa es **vis-timeline**
  8.5.4 (Apache-2.0 O MIT, activa).
- Frigate 0.17 añadió algo pequeño pero útil: **pintar de forma distinta «no hay grabación»**.

### 2.5 PTZ, presets y rondas (M)

- Por ONVIF PTZ (`ContinuousMove`, `GotoPreset`, `GetPresets`), con `onvif-zeep-async`, que ya es
  una dependencia. Por ISAPI está `/ISAPI/PTZCtrl/channels/<n>/continuous` y en Dahua `ptz.cgi`.
- Las rondas mejor **lanzarlas en la cámara** (patrol o tour nativo) que hacerlas desde el PC.
- **Ojo:** el usuario del VMS es de solo lectura (ALTA-EQUIPOS.md). El PTZ necesita un usuario con
  permiso de PTZ.
- En supermercados casi todo son domos fijos. **Pregunta a Covert** cuántos PTZ hay antes de
  invertir.

### 2.6 Permisos por cámara y por usuario (M)

- Un rol con una lista de cámaras y permisos separados para:
  - ver en vivo;
  - ver grabaciones;
  - exportar;
  - PTZ;
  - configurar.
- Ejemplo: el encargado de tienda ve en vivo, pero **no** exporta. Las cámaras de caja solo las ve
  seguridad.
- Frigate 0.17 lo añadió («custom viewer roles»), Milestone y Nx lo traen de serie, y en RGPD es
  la medida de «acceso mínimo».
- El panel central ya tiene usuarios y roles; hay que llevarlo al backend de la tienda y filtrar
  cámaras en vivo, grabaciones, instantáneas y exportación.

### 2.7 Notificaciones (S)

- Hoy hay Telegram propio. Faltan:
  - **correo**, con `smtplib` de la biblioteca estándar;
  - **webhook genérico** en JSON, con `httpx`, que ya es una dependencia, para el CRA, Teams o
    n8n de Covert.
- Hacen falta reglas por tipo (sabotaje, cámara caída, disco, cola…), por tienda y por horario, más
  **agrupación**: «5 cámaras caídas en tienda 37» en vez de 5 mensajes. Es el problema de saturación
  de avisos que se repite en los foros de Blue Iris, que tiene la opción de avisar una vez o hasta
  que vuelva la señal.
- Si se quieren decenas de servicios: **Apprise** 2.0.1, BSD-2-Clause, unas 17 500 estrellas,
  activo. Su núcleo depende de requests, click, markdown, PyYAML y certifi (MPL-2.0), todos
  permitidos. **No** instales el extra `all-plugins` (trae paho-mqtt, EPL/EDL).
- **RGPD:** por defecto los avisos **sin imagen**. La instantánea es opcional y solo para sabotaje,
  con la imagen reducida.

### 2.8 Mapas o planos (M)

- **Leaflet** 1.9.4 (BSD-2, unas 45 700 estrellas) con `CRS.Simple` y el plano de la tienda como
  imagen. Iconos de cámara con su cono de visión, color según la salud, y clic para abrir el vivo.
- En el panel central, un mapa de tiendas con la salud agregada. No hace falta mapa geográfico: con
  una lista o una cuadrícula basta, y así se evitan teselas de terceros.
- Valor medio: lo usa la central de Covert, poco la tienda.

### 2.9 Hora: NTP y detección de desfase (S)

**Cómo se lee la hora del equipo:**

| Fuente | Cómo |
|---|---|
| ONVIF | `GetSystemDateAndTime`: según la especificación responde **sin autenticación**. Para NTP, `GetNTP` |
| Hikvision | `GET /ISAPI/System/time` (`localTime`, `timeMode`) y `/ISAPI/System/time/ntpServers` |
| Dahua | `global.cgi?action=getCurrentTime` y `configManager.cgi?action=getConfig&name=NTP` |
| PC con Windows | `w32tm /query /status`, para comprobar que el propio PC está en hora |

- Se compara con la hora del PC, descontando la mitad del tiempo de ida y vuelta.
- Umbrales: **> 2 s** aviso, **> 30 s** grave. También se avisa si `timeMode` es manual.
- Cada medida se guarda (la exportación la incluye en el manifiesto).
- Recomendación de instalación: el PC de la tienda hace de servidor NTP para la VLAN de cámaras,
  porque las cámaras no tienen salida a Internet. Hay que documentarlo en `RED.md`.

### 2.10 Previsión de días de grabación (S)

- Tasa de bits real por cámara (bytes por hora en `recordings/` en las últimas 24 h) frente al disco
  libre más lo borrable. Da los **días previstos** frente al **objetivo** configurado.
- **Aviso** si los días previstos son menos que el objetivo. **Aviso RGPD** si el objetivo pasa de
  30 días (ya existe el aviso en la configuración; esto lo comprueba con datos).
- Con calculadora «¿y si añado 4 cámaras de 4 MP?» para el comercial de Covert.

### 2.11 Informe de salud (M) y salud de disco

Contenido por tienda y día, y agregado en la central:

- disponibilidad por cámara (% en línea);
- **huecos de grabación** (minutos sin segmento);
- fps y tasa de bits frente a lo esperado;
- puntuación de imagen (§3);
- desfase horario;
- días de retención reales;
- tramos protegidos;
- disco: libre, previsión y estado SMART.

Sobre el SMART:
- En Windows: `Get-PhysicalDisk` y `Get-StorageReliabilityCounter` (PowerShell nativo, sin
  dependencias).
- **No uses** smartmontools (GPL-2) ni py-SMART (LGPL-2.1, y además llama a `smartctl`).
- En Linux, para el mini PC: leer `/sys/block` y NVMe con `nvme-cli`, que es GPL y solo se
  **invocaría** (no se distribuye; si se distribuye, revisarlo). O dejarlo fuera.

En la central, la vista que vende: **«tiendas con problemas hoy»**, ordenadas por gravedad, con
exportación a PDF o CSV para el parte de mantenimiento.

### 2.12 Secuencia o rotación en los muros (S)

- Cada celda o muro puede tener una lista de cámaras con un tiempo de permanencia. Se pausa al pasar
  el ratón o al hacer doble clic. Es lo que Nx llama «Showreel», Milestone «Carousel» y HikCentral
  «Auto-switch».
- Ojo con WebRTC: hay que **precalentar** la siguiente sesión WHEP antes del cambio para que no
  salga negro.

### 2.13 Audio

**No grabar audio por defecto. Se descarta en la v2.**
- El art. 89.3 de la LOPDGDD solo admite grabar sonido en el trabajo cuando hay **riesgos
  relevantes** y siempre con proporcionalidad e intervención mínima.
- Un supermercado tiene trabajadores en todas las zonas y la AEPD ya ha sancionado casos así.
- Como mucho, más adelante: audio **en vivo** (sin grabar) en una cámara concreta, con aviso y la
  EIPD actualizada.

### 2.14 Entradas y salidas de alarma del NVR (M–L)

- **Hikvision:** `GET /ISAPI/Event/notification/alertStream` (multipart continuo):
  - `IO` (entrada de alarma);
  - `VMD` (movimiento);
  - `shelteralarm` (sabotaje, en la propia cámara);
  - `videoloss`, `diskfull`, `diskerror`, `illaccess`.
- **Dahua:** `eventManager.cgi?action=attach&codes=[All]`:
  - `AlarmLocal`;
  - `VideoMotion`;
  - `VideoBlind` (sabotaje);
  - `VideoLoss`, `StorageFailure`, `StorageLowSpace`.
- Con esto entra **también el sabotaje que detecta la propia cámara**, que complementa al nuestro
  (§3) sin coste de CPU.
- Activar salidas de relé (sirena, cerradura) por `/ISAPI/System/IO/outputs/<n>/trigger` necesita un
  usuario con permisos. Que sea opcional y quede auditado.

### 2.15 Exportación CSV de conteos (S)

Desde la página de analítica y desde el panel central, con estos datos: tienda, fecha, hora,
entradas, salidas y cola media/máxima. Usa `;` como separador y coma decimal (Excel en español),
con UTF-8 con BOM. Son datos agregados, sin datos personales.

---

## 3. IA útil, local y sin datos personales

### 3.1 Qué es cada problema y cómo se detecta

**Idea clave:** casi todo se detecta **comparando con una imagen de referencia de esa misma
cámara**, no con umbrales absolutos (cada escena es distinta). Hay dos referencias por cámara:
**día** y **noche (IR)**. Cada referencia es la **mediana de unos 15 fotogramas** tomados durante
unos minutos, para borrar a la gente que pasa.

| Problema | Visión clásica (OpenCV + NumPy) | ¿Hace falta modelo? |
|---|---|---|
| **Imagen negra** | Luma media < ~10 y % de píxeles < 8 alto | No |
| **Tapada** (mano, cinta, bolsa, spray) | Desviación típica y entropía muy bajas; casi sin bordes (Canny) respecto a la referencia | No |
| **Desenfocada / lente manchada** | Nitidez relativa: `var(Laplaciano)` / la de la referencia; bordes conservados frente a la referencia | No |
| **Movida o girada** | ORB + emparejamiento + `estimateAffinePartial2D` (RANSAC), que da el desplazamiento en px, el giro en grados y la escala. **Necesita una puerta de calidad**: si la proporción de puntos coincidentes (inliers) es baja, el resultado no vale (§3.2). Para cambios pequeños, `phaseCorrelate` | No |
| **«Mira a otro sitio»** (cambio de escena completo) | Pocos puntos coincidentes con ORB y poca coincidencia de bordes con la referencia | Clásico casi siempre. Para que aguante día/noche/estaciones, embeddings (ver abajo) |
| **Congelada** | Diferencia entre fotogramas ≈ 0 durante N fotogramas, **tapando el OSD** (el reloj sobreimpreso cambia aunque la imagen esté congelada; Hikvision lo cita en su VQD). También se puede mirar que no lleguen bytes nuevos a MediaMTX | No |
| **IR o noche fallando** | De noche se espera gris (saturación ≈ 0). Si sigue en color y oscura, el filtro IR (ICR) está atascado. Si sigue gris de día, también. Si la luma nocturna cae frente a la referencia de noche, los LED IR están fallando | No (se apoya en el horario de orto y ocaso o en la propia saturación) |
| **Suciedad, niebla, telarañas con IR** | Caen el contraste (desviación típica) y la nitidez y sube la luma (velo). De noche, manchas brillantes persistentes cerca de la lente | No (heurístico). Dar el diagnóstico exacto es difícil; se dice «calidad degradada» |
| **Contraluz o imagen quemada** | % de píxeles > 250 y < 5 a la vez (histograma bimodal) | No |
| **Dominante de color** | Distancia en a\*b\* (espacio Lab) a la referencia | No |
| **Ruido** | `skimage.restoration.estimate_sigma` (BSD-3) o la varianza en zonas planas | No |

**Para todo esto no hace falta ningún modelo nuevo.** Lo que sí usa un modelo, solo si hace falta:

- **«¿Mira donde debe?» de forma robusta** (día/noche, escaparates que cambian, temporada):
  similitud de **embeddings globales** con **DINOv2 ViT-S/14** (Apache-2.0 el código **y** los
  pesos, unos 21 M de parámetros), exportado a OpenVINO como RF-DETR. Coste: una inferencia por
  cámara **cada hora**, no por fotograma. Se recomienda **solo si** el método clásico da demasiados
  falsos positivos en el piloto. (DINOv3 tiene una licencia propia, no OSI: descartado sin revisión
  legal.)
- **Emparejamiento de puntos más robusto que ORB:** **LightGlue** (Apache-2.0) con extractores
  **ALIKED** (BSD-3) o **DISK** (Apache-2.0). **No uses SuperPoint**: su licencia es solo para
  investigación no comercial, y eso afecta también a los pesos de LightGlue entrenados para
  SuperPoint.
- **Calidad sin referencia (BRISQUE/NIQE):** no compensa. BRISQUE está en `opencv_contrib`
  (módulo `quality`, Apache-2.0), pero obliga a cambiar `opencv-python-headless` por
  `opencv-contrib-python-headless`. Además está entrenado con fotos naturales (LIVE) y puntúa mal
  la imagen IR y la de CCTV comprimida. La comparación con la referencia es mejor y gratis.
- **anomalib** (Apache-2.0, v2.6.2, activo, unas 6 200 estrellas): técnicamente vale (PatchCore o
  Dinomaly entrenados con imágenes «normales» de cada cámara, exportables a OpenVINO). Pero exige
  **entrenar por cámara** (147 tiendas × N cámaras) y trae PyTorch y Lightning. Sobra para sabotaje.
  Se guarda la idea para un futuro «¿hay algo raro en la escena?», con cuidado de RGPD.

### 3.2 Prueba rápida (OpenCV 5.0.0 del producto, MacBook M1 Pro)

Sobre el clip de pruebas del proyecto (640×360), con alteraciones sintéticas del mismo fotograma.
Referencia = mediana de 15 fotogramas.

| Caso | Señal decisiva | Valor medido | Normal |
|---|---|---|---|
| Normal (otro fotograma) | — | nitidez rel. 1,80 · bordes conserv. 0,99 · inliers 0,43 | — |
| Desenfocada | nitidez rel. / bordes conserv. | **0,00 / 0,08** | ~1 / ~1 |
| Tapada | desv. típica / entropía | **1,2 / 0,79** | 65 / 3,2 |
| Negra / IR muerto | luma / % negro | **5 / 84 %** | 127 / 0 % |
| Girada 8° | giro estimado | **−8,3°** (correcto) | 0° |
| Movida 60+25 px | desplazamiento | **65 px** (real: 65) | ~1 |
| Dominante de color | Δa\*b\* | 4,0 | 2,2 |
| Contraluz / quemada | % > 250 | 1,7 % | 0 % |
| Sucia / niebla | desv. típica / nitidez rel. | **29 / 0,35** | 65 / ~1 |
| Noche IR (gris) | saturación | **0** | 252 |
| Congelada | diferencia entre fotogramas | 0 | 14,9 |

**Coste:** de 6 a 16 ms por comprobación a 640 px, con ORB incluido. Una comprobación por cámara
cada 1–5 min no se nota ni en un N150.

**Lecciones:**
1. Con la imagen desenfocada, ORB dio un **desplazamiento falso** de 74 px con 2 % de inliers. El
   «movida o girada» solo vale si los inliers superan un mínimo (por ejemplo, 15 %). Si no, la
   causa es otra (desenfoque, tapada).
2. La dominante de color sintética salió floja (4,0 frente a 2,2) porque el clip ya está muy
   saturado. Hay que calibrarla con cámaras reales.
3. **Validez:** son alteraciones sintéticas sobre un clip de pruebas. Las alteraciones reales
   (spray, telaraña, lluvia) hay que medirlas en la fase 0 con 1 Hikvision y 1 Dahua.

### 3.3 Diseño propuesto: «Salud de imagen»

- **Dónde:** un servicio ligero en el PC de la tienda. Toma una instantánea del **subflujo** (ya
  existe `/api/cameras/{id}/snapshot`) cada 2–5 min. No decodifica de forma continua.
- **Referencias:** «Fijar referencia» la hace el instalador al dar de alta la cámara, en el
  asistente. Hay una de día y otra de noche, y se pueden volver a fijar con permiso de
  administrador y con registro en la auditoría.
- **Filtrado:** histéresis (3 comprobaciones seguidas antes de avisar). Tapar a mano zonas
  dinámicas (puertas automáticas, pantallas) y el OSD.
- **Puntuación de 0 a 100:** 100 menos penalizaciones ponderadas. Tapada, negra o congelada valen 0.
  Movida da −40 a −60 según los grados o píxeles. Desenfoque, −10 a −40 según la nitidez relativa.
  Color, ruido y contraluz, −5 a −15. Se muestran las **causas** («Desenfocada: 35 % de la nitidez
  de referencia»), no solo el número.
- **Vista «Antes / Ahora»:** la referencia junto a la actual. Es lo que convence al técnico de que
  vaya a la tienda.
- **Eventos nativos:** si la cámara ya envía `shelteralarm` o `VideoBlind` (§2.14), se suman como
  evidencia más.
- **RGPD:** solo se procesa la imagen para medir la cámara, no a las personas. La referencia es una
  mediana que borra a los transeúntes. Si aun así sale alguien (una persona quieta), se avisa en
  pantalla y se recomienda fijarla con la tienda cerrada. Las instantáneas de salud **no se
  guardan** salvo la referencia y la del último aviso.
- **Valor real:** muy alto. Con 147 tiendas, Covert se entera de una cámara tapada o girada **el
  mismo día**, y no cuando la policía pide el vídeo y no hay nada.

### 3.4 Otras ideas de IA

| Idea | Cómo | RGPD | Valor | Decisión |
|---|---|---|---|---|
| **Asistente «¿por qué no conecta?»** | Primero **reglas deterministas** en cadena: ping/TCP 80/443/554 → respuesta HTTP → autenticación (401, bloqueo) → `DESCRIBE` RTSP → códec (H.265 en el subflujo) → hora → MediaMTX. Cada paso tiene su causa probable y su acción en lenguaje claro. El LLM es **opcional** y solo redacta a partir del resultado de las reglas, sin IP ni contraseñas | Sin datos personales | Alto: reduce las llamadas a soporte | **v2** (reglas). LLM opcional |
| **Resumen de incidencias** | Ampliar el informe semanal (plantilla + LLM opcional) con salud, sabotaje, caídas y disco | Solo metadatos | Medio-alto | **v2**, coste S |
| **Búsqueda en lenguaje natural sobre metadatos** («¿qué tiendas tuvieron colas > 6 el sábado?») | Un LLM traduce a **consultas con parámetros sobre una lista cerrada** (no SQL libre). Rol de solo lectura y RLS ya existentes. Opcionalmente, un LLM local (llama.cpp, MIT, con un modelo Apache-2.0) | Solo agregados | Medio | v2.1, opcional |
| **Búsqueda semántica sobre imágenes** (CLIP, «hombre con chaqueta roja», como Frigate) | Indexa recortes de personas | **Es tratar datos personales para buscar a alguien** → choca con «nada de identificación» | — | **Descartada** |
| **Conteo de vehículos** | RF-DETR ya detecta `car/truck/bus/motorcycle` (COCO). Línea de entrada al aparcamiento con la misma canalización | Sin matrículas (**nada de ANPR**). No en el aparcamiento de personal | Bajo-medio | Opcional, solo en tiendas con aparcamiento |
| **Anonimizar la exportación** | RF-DETR detecta personas → se difumina la caja de todas menos la del interesado, marcada a mano. No hay identificación ni detector facial | Ayuda a cumplir el art. 15 (derecho de acceso sin exponer a terceros) | Medio-alto | v2.1 |
| **Medir a trabajadores** (tiempo en caja, inactividad, mapas de calor del personal) | — | Prohibido por requisito y por el art. 89 | — | **Descartado** |

---

## 4. Auditoría de seguridad de cámaras

Se vende como «Auditoría de ciberseguridad CCTV» por tienda, con un informe PDF. **Principio:** solo
equipos dados de alta por el integrador, sin explotar fallos y sin probar contraseñas. Cada
comprobación queda en `audit.log`.

### 4.1 Comprobaciones

| Comprobación | Cómo, sin atacar | Notas |
|---|---|---|
| **Contraseña guardada débil o de fábrica** | Se analiza **localmente** la contraseña que el usuario ya guardó en el VMS: longitud ≥ 8, ≥ 2–3 tipos de carácter, que no esté en una lista corta de valores de fábrica o triviales (`12345`, `123456`, `admin`, `888888`, `666666`, `password`…) y que no sea el nombre de usuario. **Ni una petición de red** | Sin fuerza bruta. Se avisa también si el VMS usa `admin` en vez del usuario de solo lectura recomendado |
| **Acceso anónimo** | Un `DESCRIBE` RTSP **sin** credenciales y un `GetProfiles` ONVIF sin credenciales. Si responden 200, es un fallo grave | Una sola petición, no hay explotación |
| **Servicios inseguros** | Conexión TCP a 23 (Telnet), 22 (SSH), 80 sin redirección a 443, 443 (¿hay TLS?), 8000 y 37777 (SDK), 1900/UDP (SSDP: una búsqueda UPnP pasiva desde el PC). Con un usuario **administrador opcional** para el modo auditoría: Hikvision `/ISAPI/System/Network/telnetd`, `/ISAPI/System/Network/UPnP`, `/ISAPI/System/Network/EZVIZ` (Hik-Connect/P2P) y `/ISAPI/Security/adminAccesses` (protocolos y puertos). Dahua: `configManager.cgi?action=getConfig&name=Telnet`, `UPnP`, `T2UServer` (P2P) y `Web` | El usuario del VMS es de solo lectura, así que esas lecturas **piden credenciales de administrador temporales** (no se guardan). Sin ellas, solo se informa lo que se ve desde fuera (puertos, SSDP) |
| **Firmware con CVE** | El modelo y el firmware ya los lee el alta. Falta leer `firmwareReleasedDate` en Hikvision (`build 210628`), que hoy no se guarda | Ver §4.2 |
| **Hora mal puesta** | §2.9 | Afecta a la validez de la prueba |
| **Cámaras detrás del NVR** | Las cámaras en los puertos PoE del NVR no se ven desde el PC. Se audita el NVR y, si su API lo da, el modelo y el firmware de cada canal (en Hikvision, `/ISAPI/ContentMgmt/InputProxy/channels`; **a verificar** con el equipo real) | — |

### 4.2 Fuentes de datos de CVE y cómo cruzarlas con modelo y firmware

| Fuente | Licencia o condiciones | Para qué |
|---|---|---|
| **NVD API 2.0** | Datos del Gobierno de EE. UU. de uso libre. Pide la nota *«This product uses the NVD API but is not endorsed or certified by the NVD»*. Límite: 5 peticiones/30 s sin clave y 50 con clave gratuita | CVSS, descripción, CPE |
| **CISA KEV** (`known_exploited_vulnerabilities.json`, repo `cisagov/kev-data`) | **CC0** | Marcar «explotada activamente». A 4/10/2026 incluye **Hikvision CVE-2021-36260** (desde 2022), **CVE-2017-7921** (desde el 5/3/2026) y **Dahua CVE-2021-33044 y CVE-2021-33045** (desde el 21/8/2024), además de NUUO, TVT, Amcrest, Reolink… |
| **EPSS (FIRST)** | Gratis, sin registro; piden atribución | Probabilidad de explotación, para ordenar |
| **Avisos de los fabricantes** (Hikvision HSRC y PSIRT de Dahua) | Páginas públicas (se usan como dato, no se copia su texto) | **Las versiones afectadas reales** |
| cvelistV5 (MITRE) | Condiciones de uso de CVE (libre) | Alternativa a NVD |
| ~~cve-search~~ | **AGPL-3.0** | Descartado |

**Hallazgo importante:** NVD **por sí sola no sirve** para Hikvision.
- **Hikvision:** CVE-2021-36260 tiene **610 CPE**, todos con la versión `-` (sin rango de versiones:
  `cpe:2.3:o:hikvision:ds-2cd2026g2-iu\/sl_firmware:-`). La versión que corrige el fallo solo
  está en el aviso de Hikvision: firmware con **build anterior a 210628** (y por familias: IPC_G3,
  IPC_H5, IPC_E2…).
- **Dahua:** sí trae rangos, pero por **familia con comodines** (`ipc-hx5xxx_firmware`, versión <
  `2.820.0000000.18.r.210705`).

**Cómo cruzarlo:**
1. Una **tabla propia mantenida a mano** (`advisories.json`, la publica la central y se firma) con:
   fabricante, patrón del modelo (expresión regular o familia), cómo comparar (Hikvision: fecha del
   build; Dahua: versión numérica por partes y fecha), versión que corrige, CVE, aviso del
   fabricante y fecha de revisión.
2. La central añade CVSS (NVD), el indicador KEV y EPSS una vez al día y lo envía a las tiendas
   por la VPN. **Las tiendas no salen a Internet.**
3. Resultado por equipo: *«Vulnerable (KEV)»*, *«Probablemente vulnerable (familia coincide;
   confirmar)»*, *«Sin CVE conocidos en la tabla»* o *«Desconocido»*. **Nunca se dice «seguro».**
4. Para empezar basta con los 10–20 CVE de Hikvision y Dahua de KEV y de alta gravedad. Revisión
   trimestral.

---

## 5. Onboarding para usuarios no técnicos

### 5.1 Bibliotecas de recorridos guiados (verificadas el 5/10/2026)

| Biblioteca | Licencia | Estado | Decisión |
|---|---|---|---|
| **Driver.js** (`nilbuild/driver.js`; antes `kamranahmedse/driver.js`, redirige) | **MIT** (comprobado en LICENSE y en npm) | v1.9.0 publicada el **3/10/2026**, unas 26 900 estrellas, **sin dependencias** | **Elegida.** JS puro, encaja con las páginas estáticas actuales y con la CSP. Se copia en `vms/web/vendor/` (sin CDN) |
| boarding.js | MIT | Fork de Driver.js, unas 150 estrellas | Reserva |
| tourguide-js (`sjmc11`) | MIT | Último cambio en mayo de 2025 | No (poco activo) |
| reactour | MIT | Solo React | No (no usamos React) |
| **Shepherd.js** | **AGPL-3.0 + licencia comercial de pago** (LICENSE.md: el uso comercial *requiere* comprarla; npm 15.3.0: `AGPL-3.0`) | Activo | **Descartada** |
| **Intro.js** | **AGPL-3.0 + comercial** (npm 8.6.0: `AGPL-3.0`) | Activo | **Descartada** |

### 5.2 Asistente de primer uso (M)

Aparece cuando no hay ningún equipo. Se puede cerrar y retomar desde «Ayuda».

1. **Bienvenida:** qué vas a conseguir («ver tus cámaras en los monitores y que se graben») y cuánto
   tarda (unos 10 min).
2. **Buscar en la red:** descubrimiento ONVIF (ya existe) más «Escribir la IP a mano». Explica qué
   es un NVR en una frase.
3. **Credenciales:** recomienda el usuario de solo lectura, con un enlace a cómo crearlo en
   Hikvision o Dahua. Si es `admin`, lo avisa (§4.1).
4. **Probar conexión:** con el asistente de diagnóstico (§3.4) en lenguaje claro: «La cámara
   responde pero rechaza la contraseña. Ojo: tras 5 intentos fallidos Hikvision bloquea el
   usuario 30 min».
5. **Elegir canales:** con miniaturas. Avisa si el subflujo no es H.264.
6. **Fijar referencia de imagen** (§3.3), en un clic.
7. **Asignar al muro:** arrastrar a la cuadrícula del monitor 1, con vista previa.
8. **Grabación:** días objetivo con la previsión de días real (§2.10) y el aviso RGPD si pasa de
   30.
9. **Listo:** resumen y «¿Quieres un recorrido de 1 minuto por la pantalla de reproducción?»
   (Driver.js).

### 5.3 Ayuda contextual y estados vacíos

- **Botón «?»** en cada sección. Abre un panel lateral con 3–5 líneas, un enlace al apartado del
  manual y «Ver recorrido» (Driver.js). El texto va en archivos de traducción, para el catalán y el
  inglés más adelante.
- **Estados vacíos** según las 3 pautas de NN/g:
  - decir el estado del sistema («No hay grabaciones de esta cámara entre 10:00 y 12:00»);
  - enseñar («Las grabaciones aparecen aquí cuando…»);
  - dar el **camino directo** («Añadir cámara», «Ir a Grabación»).
- **Recorridos solo para quien configura** (administrador o técnico) y **nunca en los muros en
  kiosco**: un operador no puede tener una ventana tapando el vídeo. Se guarda «visto» por
  usuario.

### 5.4 Buenas prácticas de UX en software de seguridad

- **El estado no se indica solo con color:** icono + texto («Sin señal desde 10:42»). Por el
  daltonismo y porque el monitor puede estar lejos.
- **Contra la saturación de avisos:** gravedades claras (crítico, aviso, informativo), agrupación y
  «silenciar 1 h con motivo».
- **Errores que dicen qué hacer**, no códigos («401» → «Usuario o contraseña incorrectos»).
- **Acciones con riesgo** (borrar equipo, cambiar retención, proteger o desproteger evidencia,
  exportar): pedir confirmación y motivo, y que queden auditadas.
- **Verificar siempre con un botón «Probar»** antes de guardar (cámaras, Telegram, correo,
  webhook, NTP).
- **Lenguaje de tienda, no de ingeniero:** «flujo secundario (calidad baja para el muro)» antes que
  «substream».
- **Mismo vocabulario** en la tienda, la central y los informes.

---

## 6. Proyectos open source cuyo código o datos se pueden reutilizar

Verificado el 5/10/2026 (estrellas aproximadas, último *push*).

| Proyecto | ★ | Licencia | Actividad | Qué tomaríamos |
|---|---|---|---|---|
| [Driver.js](https://github.com/nilbuild/driver.js) | 26,9 k | MIT | v1.9.0, 03/10/2026 | Recorridos guiados (§5) |
| [Apprise](https://github.com/caronc/apprise) | 17,5 k | BSD-2 | v2.0.1, 03/10/2026 | Notificaciones a decenas de servicios (opcional, §2.7) |
| [Leaflet](https://github.com/Leaflet/Leaflet) | 45,7 k | BSD-2 | 02/10/2026 | Planos de tienda con `CRS.Simple` (§2.8) |
| [vis-timeline](https://github.com/visjs/vis-timeline) | 2,6 k | Apache-2.0 o MIT | 8.5.4, 03/10/2026 | Solo si la línea de tiempo propia se queda corta (§2.4) |
| [OpenCV](https://github.com/opencv/opencv) (ya en el producto, 5.0.0) | — | Apache-2.0 | activo | ORB, `estimateAffinePartial2D`, `phaseCorrelate`, Laplaciano, histogramas: todo el §3 |
| [scikit-image](https://github.com/scikit-image/scikit-image) | 6,6 k | BSD-3 | 01/10/2026 | `estimate_sigma` (ruido), SSIM si hiciera falta |
| [imagehash](https://github.com/JohannesBuchner/imagehash) | 3,9 k | BSD-2 | 26/09/2026 | Hash perceptual para «congelada» y «cambio de escena» rápidos (opcional) |
| [DINOv2](https://github.com/facebookresearch/dinov2) | 13,4 k | Apache-2.0 (código y pesos) | 06/2026 | Embeddings para «¿mira donde debe?» robusto (solo si el clásico falla) |
| [LightGlue](https://github.com/cvg/LightGlue) + [ALIKED](https://github.com/Shiaoming/ALIKED) / [DISK](https://github.com/cvlab-epfl/disk) | 4,8 k / 0,4 k / 0,4 k | Apache-2.0 / BSD-3 / Apache-2.0 | LightGlue 02/2026; ALIKED y DISK quietos | Emparejamiento robusto si ORB no basta (**no** SuperPoint) |
| [anomalib](https://github.com/open-edge-platform/anomalib) | 6,2 k | Apache-2.0 | v2.6.2, 03/10/2026 | Nada en la v2; idea para «escena anómala» más adelante |
| [OpenVINO](https://github.com/openvinotoolkit/openvino) (ya en el producto) | 10,9 k | Apache-2.0 | activo | Ejecutar DINOv2 si se adopta |
| [Frigate](https://github.com/blakeblackshear/frigate) | 36,4 k | MIT | v0.18.0, 12/09/2026 | **Ideas, no código** (el plan lo prohíbe y su nombre no se puede usar): búsqueda por movimiento con índice, casos de exportación, roles por cámara, asistente de alta, «sin grabación» en la línea de tiempo |
| [Viseron](https://github.com/roflcoopter/viseron) | 3,6 k | MIT | 04/10/2026 | Ideas de su interfaz de eventos |
| [cameradar](https://github.com/Ullaakut/cameradar) | 5,2 k | MIT | 29/09/2026 | Solo su **diccionario de rutas RTSP** por marca (para «¿qué ruta usa esta cámara?»). **No** su fuerza bruta de credenciales |
| [cisagov/kev-data](https://github.com/cisagov/kev-data) | 0,1 k | **CC0** | diario | Catálogo KEV (§4.2) |
| [CVEProject/cvelistV5](https://github.com/CVEProject/cvelistV5) | 3 k | Condiciones de uso de CVE | diario | Alternativa a la API de NVD |
| [c2pa-python](https://github.com/contentauth/c2pa-python) / [c2pa-rs](https://github.com/contentauth/c2pa-rs) | 0,1 k / 0,4 k | Apache-2.0 / MIT o Apache | activos | Futuro: credenciales de contenido en la exportación |
| [mp4box.js](https://github.com/gpac/mp4box.js) | 2,5 k | BSD-3 | 23/09/2026 | Si el visor portable tuviera que leer fMP4 directamente |
| [video.js](https://github.com/videojs/video.js) | 39,9 k | Apache-2.0 | activo | Probablemente innecesario: `<video>` nativo basta en el visor |

Los repos de «camera tampering detection» de GitHub son muy pequeños (≤ 13 estrellas) y **sin
licencia**. Solo valen como idea; el método (§3) se escribe desde cero con OpenCV.

---

## 7. Qué descartar y por qué

| Descartado | Motivo |
|---|---|
| **pyiqa / IQA-PyTorch** | Licencia **PolyForm Noncommercial 1.0.0** (comprobada en el repo y en PyPI 0.1.16). Prohibida en producto comercial |
| **piq** | Apache-2.0, pero **inactivo desde mayo de 2024** y requiere PyTorch en la tienda |
| **BRISQUE/NIQE** | Obliga a cambiar a la wheel *contrib* de OpenCV y funciona mal con IR y CCTV comprimido. La comparación con la referencia es mejor |
| **pybrisque** | GPL-3.0 |
| **anomalib para sabotaje** | Hay que entrenar por cámara (cientos), trae PyTorch y Lightning. Lo clásico resuelve lo mismo por casi nada |
| **SuperPoint** (y LightGlue con pesos de SuperPoint) | Licencia de investigación no comercial |
| **DINOv3** | Licencia propia no OSI; sin revisión legal, no |
| **Shepherd.js, Intro.js** | AGPL-3.0 + licencia comercial de pago |
| **cve-search** | AGPL-3.0 |
| **python-nmap / nmap**, **smartmontools**, **py-SMART** | GPL / GPL / LGPL que llama a un binario GPL. Para puertos basta con sockets de asyncio; para el disco, PowerShell nativo |
| **libx264** para quemar la marca de agua | GPL. Usa la marca en el visor, o OpenH264, QSV o NVENC para una copia de trabajo |
| **Grabar audio** | Art. 89.3 LOPDGDD: solo con riesgos relevantes y proporcionalidad; en supermercado con plantilla, no por defecto |
| **Búsqueda semántica sobre imágenes (CLIP), reconocimiento facial, re-identificación, ANPR** | Identifican o buscan a personas. Fuera por requisito |
| **Cualquier métrica de trabajadores** (tiempo en caja, inactividad, mapas de calor del personal) | Requisito del cliente y art. 89 LOPDGDD |
| **Fuerza bruta o diccionario contra los equipos** (incluido el de cameradar) | Riesgo legal y operativo: Hikvision y Dahua bloquean el usuario tras varios fallos. Solo se analizan **localmente** las credenciales ya guardadas |
| **Exploits o PoC de CVE** (HikPwn GPL, «scanners» sin licencia) | No se comprueba si un fallo es explotable: se deduce de la versión |
| **C2PA en la v2** | No lo pide nadie en la cadena policial todavía; el manifiesto firmado + SHA-256 cubre lo que piden las guías |
| **PTZ completo y mapas en tienda** antes de saber si Covert los usa | Esfuerzo M sin demanda confirmada |

---

## 8. Preguntas para Covert (antes de cerrar el alcance)

1. ¿Cuántos PTZ hay por tienda? ¿Usan rondas?
2. ¿Las cámaras cuelgan de los puertos PoE del NVR (no se ven desde el PC) o están en la LAN?
3. ¿Qué piden hoy los Mossos o la Policía Local al retirar vídeo (formato, reproductor, acta)?
4. ¿Tienen CRA o una herramienta de tickets a la que mandar webhooks?
5. ¿Hay entradas de alarma cableadas a los NVR (intrusión, botón de pánico)?
6. ¿Venderían la auditoría de ciberseguridad como servicio aparte? (Así se decide si va en la v2.)
7. ¿Cuántas tiendas tienen aparcamiento propio con cámara?

---

## Fuentes

**Productos comerciales:**
- [Milestone: verificar firmas digitales en el Smart Client Player](https://doc.milestonesys.com/2025r2/en-US/standard_features/sf_sc/sf_player/sc_playerdigitalsignaturesverify.htm)
- [Milestone: activar la firma digital en la exportación](https://doc.milestonesys.com/latest/en-US/standard_features/sf_mc/sf_mcnodes/sf_2serversandhardware/mc_enabledigitalsigningforexport.htm)
- [Milestone: exportar evidencias](https://doc.milestonesys.com/2024r2/en-US/standard_features/sf_sc/sf_investigate/current/sc_exportingevidence.htm)
- [Nx Witness: historial de versiones (marca de agua, exportación)](https://updates.networkoptix.com/default/index.html)
- [Nx Witness: manual de usuario (Smart Motion Search)](https://netcamcenter.nl/media/documents/Nx-Witness-5.0-User-Manual-compressed_2023-08-16-133212_zzds.pdf)
- [Hikvision: cómo configurar Video Quality Diagnosis](https://www.hikvision.com/content/dam/hikvision/en/support/how-to/how-to-document/network-cameras/How-to-Configure-Video-Quality-Diagnosis-Function.pdf)
- [Dahua Wiki: DSS (VQDS)](https://dahuawiki.com/images/cache/e/e7/DSS.html)
- [Genetec: KiwiVision Camera Integrity Monitor](https://techdocs.genetec.com/r/en-US/KiwiVisionTM-User-Guide-for-Security-Center-5.12.0.0/About-the-KiwiVision-Camera-Integrity-Monitor-module)
- [Genetec: automatizar el mantenimiento con Camera Integrity Monitor](https://resources.genetec.com/omnicast-video-surveillance/automating-maintenance-with-the-kiwivision-camera-integrity-monitor)
- [Blue Iris: watchdog](https://www.houselogix.com/docs/blue-iris/BlueIris/watchdog.htm)

**Frigate y foros:**
- [Frigate: discusión #3012 (sabotaje y errores de cámara)](https://github.com/blakeblackshear/frigate/discussions/3012)
- [Frigate: discusión #7443 (cámara tapada)](https://github.com/blakeblackshear/frigate/discussions/7443)
- [Frigate: revisión y búsqueda por movimiento](https://docs.frigate.video/usage/review)
- Notas de las versiones 0.17.0 y 0.18.0 de Frigate (GitHub Releases).
- [IPCamTalk: notificaciones del watchdog](https://ipcamtalk.com/threads/watchdog-notifications.63659/)
- [IPVM: problemas de hora entre Exacq y Hikvision](https://ipvm.com/discussions/time-sync-issues-exacq-and-hikvision-cameras)
- [IPCamTalk: hora incorrecta en cámaras Hikvision](https://ipcamtalk.com/threads/eyesurv-hikvision-cameras-incorrect-time-and-resetting-time-configuration.1657/)

**Normativa y requisitos policiales:**
- [UK Police Requirements for CCTV v3.0](https://assets.publishing.service.gov.uk/media/65f4b481811225001a579f80/UK_Police_Requirements_for_CCTV_v3.0.pdf)
- [Recovery and Acquisition of Video Evidence v3.0](https://assets.publishing.service.gov.uk/media/62a9f18fd3bf7f036bb12944/Recovery_and_Acquisition_of_Video_Evidence_v3-0.pdf)
- [AEC DPD: imágenes como prueba fuera del plazo (art. 22 LOPDGDD)](https://dpd.aec.es/las-imagenes-de-videovigilancia-como-prueba-judicial-fuera-del-plazo-de-conservacion/)
- [Art. 89 LOPDGDD](https://www.conceptosjuridicos.com/articulos/ley-organica-de-proteccion-de-datos-articulo-89/)
- [BOE: el límite entre vigilar y escuchar (audio)](https://www.boe.es/biblioteca_juridica/anuarios_derecho/articulo.php?id=ANU-L-2025-00000003163)

**CVE y datos de vulnerabilidades:**
- [CISA KEV (JSON)](https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json)
- [cisagov/kev-data](https://github.com/cisagov/kev-data)
- API de NVD 2.0: `https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=…`
- [NVD: empezar con la API](https://nvd.nist.gov/developers/start-here)
- [Hikvision: herramienta de búsqueda de firmware para CVE-2021-36260](https://www.hikvision.com/us-en/support/cybersecurity/security-advisory/security-notification-command-injection-vulnerability-in-some-hikvision-products/security-notification-command-injection-vulnerability-in-some-hikvision-products/search-tool-for-important-firmware-update/)
- [watchfulIP: análisis de CVE-2021-36260](https://watchfulip.github.io/2021/09/18/Hikvision-IP-Camera-Unauthenticated-RCE.html)
- [EPSS: datos](https://www.first.org/epss/data)
- [EPSS: preguntas frecuentes](https://www.first.org/epss/faq)

**API de los equipos:**
- [Hikvision ISAPI (guía general)](https://download.isecj.jp/catalog/misc/isapi.pdf)
- [API HTTP de cámaras Dahua](https://community.jeedom.com/uploads/short-url/tTQJPaNah7gZnU12VGGN9ZHEhOk.pdf)
- [Dahua: solución de problemas de P2P](https://dahuatech.zendesk.com/hc/en-gb/articles/12228822283666-IPC-NVR-XVR-P2P-Issues-Troubleshooting)

**UX:**
- [NN/g: estados vacíos en aplicaciones complejas](https://www.nngroup.com/articles/empty-state-interface-design/)
- [Carbon Design System: patrón de estados vacíos](https://carbondesignsystem.com/patterns/empty-states-pattern/)

**Licencias:** comprobadas con `gh api repos/<repo>` y `/license`, PyPI JSON (apprise, anomalib,
pyiqa) y el registro de npm (driver.js, shepherd.js, intro.js, leaflet, vis-timeline) el 5/10/2026.
