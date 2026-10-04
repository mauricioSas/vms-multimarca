# Alta de cámaras y NVR Hikvision, Dahua y ONVIF

Esta guía explica cómo preparar cada equipo **antes** de darlo de alta en VMS Multimarca y cómo
comprobar con VLC que el vídeo llega. Si VLC no ve la cámara, el VMS tampoco la verá: empieza
siempre por ahí.

## Resumen: lo que tiene que cumplir cada equipo

| Requisito | Por qué |
|---|---|
| RTSP activado (puerto 554 por defecto) | es por donde viaja el vídeo |
| Usuario **de solo lectura** propio para el VMS | no usar `admin`; si se filtra, no permite cambiar nada |
| **Subflujo en H.264** (no H.265 ni H.265+) | los navegadores no reproducen H.265 por WebRTC; el subflujo es el de la vista en vivo |
| Flujo principal: H.264 o H.265 | se graba tal cual; H.265 ahorra disco (la reproducción descargada funciona; en el navegador depende del equipo) |
| Desactivar «H.264+ / H.265+ / Smart Codec» | generan cambios de GOP que dan imagen gris o cortes en WebRTC |
| GOP (intervalo de I-frame) ≤ 2× fps (p. ej. 25 o 50 con 25 fps) | la vista en vivo arranca antes |
| Hora por NTP y zona horaria correcta | las grabaciones y conteos se cuadran por hora |
| ONVIF activado (si el alta es por ONVIF o para el descubrimiento) | — |
| Firmware actualizado | corrige fallos de RTSP y de seguridad |

Valores de subflujo recomendados: 640×360 o 704×576 (D1), 10–15 fps, 512–1024 kbps, H.264
perfil Main o Baseline. Para la cámara de la **puerta con analítica**, sube el subflujo a 12–15
fps (el seguimiento de personas lo necesita); para la de **cajas**, 5–10 fps bastan.

## Hikvision (cámaras y NVR)

Interfaz web: `http://IP-DEL-EQUIPO` (Internet Explorer ya no hace falta en firmware actuales).

### Activar RTSP y ONVIF
1. **Configuración → Red → Configuración avanzada → Protocolo de integración**
   (*Network → Advanced Settings → Integration Protocol*).
2. Marca **Habilitar Open Network Video Interface (ONVIF)** y, en el mismo apartado, crea un
   **usuario ONVIF** con rol **Media User** (solo lectura). Guarda.
3. **Configuración → Red → Configuración básica → Puerto**: anota el **puerto RTSP** (554).
4. **Configuración → Sistema → Seguridad → Autenticación**: *RTSP Authentication* = **digest**
   (o «digest/basic»).

### Usuario de solo lectura
**Configuración → Sistema → Gestión de usuarios → Añadir**: tipo **Operador** o **Usuario**,
contraseña robusta, y en permisos deja solo **Vista en directo** y **Reproducción** (en un NVR,
para todos los canales). Desmarca configuración, PTZ, actualización, etc.

### Subflujo en H.264
**Configuración → Vídeo/Audio → Vídeo** (en un NVR: **Cámara → Parámetros de vídeo**, canal a canal):
- Tipo de flujo: **Subflujo** → Códec de vídeo **H.264**, H.264+ **desactivado**, resolución
  640×360 o 704×576, fps 12–15, bitrate 768 kbps, intervalo de I-frame 25–30.
- Flujo principal: H.264 o H.265, H.265+ **desactivado**.

### Rutas RTSP (las pone el VMS solo)
```
rtsp://usuario:contraseña@IP:554/Streaming/Channels/101   canal 1, flujo principal
rtsp://usuario:contraseña@IP:554/Streaming/Channels/102   canal 1, subflujo
rtsp://usuario:contraseña@IP:554/Streaming/Channels/201   canal 2, flujo principal (NVR)
```

## Dahua (cámaras, NVR y XVR)

Interfaz web: `http://IP-DEL-EQUIPO`.

### Activar RTSP y ONVIF
1. **Configuración → Red → Puerto**: RTSP **554** activado.
2. **Configuración → Red → Plataforma de acceso → ONVIF** (o *Integration Protocol*): **Habilitar**.
   En firmware recientes, en **Seguridad → Servicio del sistema** activa también
   «Autenticación ONVIF» y comprueba que **RTSP over TLS** está desactivado (el VMS usa RTSP normal
   dentro de la VLAN de cámaras).
3. **Seguridad → Servicio del sistema**: deja activado «Autenticación digest» para RTSP.

### Usuario de solo lectura
**Sistema → Cuenta → Grupo**: crea un grupo con permisos solo de **Vista en directo** y
**Reproducción** para los canales necesarios. **Sistema → Cuenta → Usuario → Añadir**: asígnale ese
grupo. Ojo: Dahua bloquea el usuario unos minutos tras varios intentos fallidos (5 por defecto);
si te equivocas de contraseña al probar, espera antes de reintentar.

### Subflujo en H.264
**Cámara → Codificación → Codificación de vídeo** (*Encode*):
- **Sub Stream 1**: compresión **H.264** (o H.264H), Smart Codec **desactivado**, 640×360/704×576,
  12–15 fps, bitrate 768 kbps, intervalo de I-frame igual a los fps o el doble.
- **Main Stream**: H.264 o H.265, Smart Codec **desactivado**.

### Rutas RTSP (las pone el VMS solo)
```
rtsp://usuario:contraseña@IP:554/cam/realmonitor?channel=1&subtype=0   canal 1, principal
rtsp://usuario:contraseña@IP:554/cam/realmonitor?channel=1&subtype=1   canal 1, subflujo
```

## ONVIF genérico (otras marcas)

Activa ONVIF y crea un usuario ONVIF de tipo **Media/Operator**. En el VMS elige fabricante
**ONVIF**: el alta consulta los perfiles (`GetProfiles`/`GetStreamUri`) y guarda las rutas RTSP
que devuelve la cámara. Si la cámara no tiene ONVIF, usa **Genérico (RTSP manual)** y escribe las
rutas del manual del fabricante.

## NVR: ¿conectar el NVR o cada cámara?

| Opción | Ventajas | Inconvenientes |
|---|---|---|
| **A través del NVR** (recomendado si el NVR es reciente) | un solo usuario, un alta, rutas homogéneas; el NVR sigue grabando como respaldo | el NVR limita el ancho de banda de salida (revisa «ancho de banda saliente» en sus características) |
| Cámara a cámara | sin límite del NVR | más altas y usuarios; las cámaras en el PoE del NVR no son accesibles desde la red |

Las cámaras conectadas a los puertos PoE del NVR quedan en una red interna del NVR: hay que
acceder **a través del NVR**.

**Importante:** el VMS abre **una sola conexión por canal y flujo** (MediaMTX la reparte entre
todos los monitores y la analítica), así que no multiplica la carga del NVR.

## Comprobar el RTSP con VLC (5 minutos por equipo)

1. Instala VLC (videolan.org) en un portátil conectado a la red de cámaras.
2. **Medio → Abrir ubicación de red** y pega la URL, por ejemplo:
   `rtsp://visor:MiClave@192.168.10.21:554/Streaming/Channels/102`
   Si la contraseña tiene `@ : / # ? %`, escríbelos codificados (`@` → `%40`, `#` → `%23`,
   `:` → `%3A`, `/` → `%2F`, `?` → `%3F`, `%` → `%25`). El VMS ya lo hace solo.
3. Si ves la imagen: **Herramientas → Información del códec**. Comprueba:
   - Subflujo: **H264 - MPEG-4 AVC (part 10)**. Si pone **HEVC/H.265**, cámbialo en el equipo.
   - Resolución y fps esperados.
4. Para forzar TCP (como el VMS): **Herramientas → Preferencias → Mostrar ajustes: Todo →
   Entrada/Códecs → Demultiplexores → RTP/RTSP → Usar RTP sobre RTSP (TCP)**.
5. Errores típicos: «401 Unauthorized» (usuario o contraseña), «454 Session Not Found» o
   «404» (ruta o canal mal), imagen gris (ver [PROBLEMAS.md](PROBLEMAS.md)).

## Dar de alta en VMS Multimarca

1. Entra como administrador en `http://IP-DEL-PC:8600/`.
2. **Equipos → Buscar en la red** (descubrimiento ONVIF/WS-Discovery) o **Añadir equipo**.
3. Fabricante, IP, puertos (HTTP 80 / RTSP 554), usuario de solo lectura y contraseña →
   **Probar conexión**. Verás el modelo, el número de canales y si el RTSP responde.
4. Elige los canales a importar. Cada canal pasa a ser una cámara con su flujo principal (se graba
   24/7) y su subflujo (vista en vivo y analítica).
5. Si un canal muestra el aviso «El subflujo es H.265…», cámbialo a H.264 en el equipo.
6. Coloca las cámaras en los muros (Muros → Monitor 1…4 → cuadrícula 1/4/9/16).

Las contraseñas se guardan cifradas en el almacén de credenciales; nunca aparecen en la
configuración, en la interfaz ni en los registros.
