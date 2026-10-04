# Checklist de pruebas con hardware real

Las pruebas automáticas cubren el código con simuladores (cámaras RTSP con rutas reales de
Hikvision/Dahua, mocks ISAPI/CGI/ONVIF, PostgreSQL embebido, navegador real). Lo que **solo** se
puede comprobar con equipos reales está en esta lista. Marca cada punto y anota el resultado
(modelo, firmware, cifras) para el acta de pruebas.

Datos de la prueba: fecha ____ · tienda/lugar ____ · técnico ____ · versión del VMS ____

## A. Instalación (Windows)

- [ ] `install.ps1` termina sin errores en Windows 10 y en Windows 11 (anota cuál).
- [ ] Con Python ya instalado (venv) y sin Python (embebible): ambos modos funcionan.
- [ ] `Get-Service VMS*` → `Running`; tras **reiniciar el PC** los servicios arrancan solos.
- [ ] Mata el proceso `python.exe` del backend en el Administrador de tareas: el servicio vuelve
      en ≤ 10 s y MediaMTX no queda duplicado (`Get-Process mediamtx` → uno).
- [ ] `Stop-Service VMSBackend` no deja `mediamtx.exe` huérfano.
- [ ] Reglas del firewall solo en perfil Privado; desde otro PC de la LAN se abre `:8600`; desde
      una red pública/invitados **no**.
- [ ] `C:\ProgramData\VMSMultimarca\.env` y `secrets\` no se pueden abrir con un usuario normal.
- [ ] `uninstall.ps1` sin `-RemoveData` conserva grabaciones; reinstalar las recupera.

## B. Alta de equipos

Por cada modelo (anota modelo y firmware):

| Equipo | Descubrimiento | Prueba de conexión | Canales importados | Subflujo H.264 | Notas |
|---|---|---|---|---|---|
| NVR Hikvision ____ | | | | | |
| Cámara Hikvision ____ | | | | | |
| NVR/XVR Dahua ____ | | | | | |
| Cámara Dahua ____ | | | | | |
| Cámara ONVIF otra marca ____ | | | | | |

- [ ] Contraseña con caracteres especiales (`@ # : / ? %`) funciona.
- [ ] Contraseña errónea: mensaje claro y **no** bloquea el usuario del equipo (un solo intento).
- [ ] Canal desconectado en el NVR: aparece como sin vídeo, el resto sigue.
- [ ] Aviso «subflujo H.265» aparece si el subflujo está en H.265.

## C. Vista en vivo en 4 monitores

- [ ] `install-kiosk.ps1`: al iniciar sesión se abre un muro por monitor, cada uno en **su**
      pantalla (anota la GPU y si hubo que mover alguna ventana: ver `kiosk.log`).
- [ ] Muros 1/4/9/16; doble clic → pantalla completa con flujo principal; volver.
- [ ] 4 muros × 16 cámaras durante **1 hora**: CPU ____ %, GPU ____ %, RAM ____ GB; sin cortes.
- [ ] Retraso extremo a extremo (cronómetro delante de la cámara): ____ ms (objetivo < 1 s).
- [ ] Cierra una ventana con Alt+F4: se reabre sola.
- [ ] Desenchufa un NVR 2 minutos y vuelve a enchufarlo: las celdas muestran «Sin señal» y
      recuperan el vídeo solas (anota el tiempo: ____ s).
- [ ] Reinicia el router: el vídeo vuelve sin tocar nada.
- [ ] Deja el PC 24 h: la pantalla no se apaga, los muros siguen (la sesión del kiosco se renueva).
- [ ] Acceso remoto por la VPN con `VMS_MTX_WEBRTC_ADDITIONAL_HOSTS`: vídeo en vivo funciona.

## D. Grabación y reproducción

- [ ] Grabación 24/7 de todas las cámaras durante 48 h sin huecos (línea de tiempo continua).
- [ ] Corte de luz brusco (desenchufar): al volver se reproduce hasta ≤ 2 s antes del corte.
- [ ] Reproducción con salto en la línea de tiempo; descarga de un clip de 10 min reproducible en
      VLC y en el reproductor de Windows.
- [ ] Bitrate real medido por cámara: ____ Mbps → GB/día ____ (comparar con
      [REQUISITOS-HARDWARE.md](REQUISITOS-HARDWARE.md)).
- [ ] Retención: con `days=1` los segmentos de más de 24 h desaparecen.
- [ ] Disk guard: con el umbral bajado por debajo del uso actual, borra lo más antiguo y lo registra.

## E. Analítica (piloto)

- [ ] Cámara de puerta cenital: 200 cruces contados a mano frente al sistema: entradas ____ /
      ____ (error ____ %), salidas ____ / ____. Objetivo: error < 5 %.
- [ ] Prueba con grupos (2–3 personas juntas), carros y niños.
- [ ] Zona de cajas: ocupación comparada con conteo manual cada minuto durante 30 min.
- [ ] Alerta de cola: llega el Telegram al superar el umbral el tiempo configurado; el fin de la
      alerta se registra; no se repite antes del tiempo de espera.
- [ ] Mini PC (N150/i5): CPU ____ %, fps procesados puerta ____ / cajas ____, temperatura ____ °C
      tras 8 h.
- [ ] Corte de la VPN 30 min: los conteos se acumulan en local y se envían al volver (sin huecos
      en el panel central).
- [ ] **RGPD**: en el mini PC no hay imágenes ni vídeo de la analítica (busca `*.jpg`, `*.png`,
      `*.mp4` en la carpeta de datos fuera de `recordings`).

## F. Panel central y latido

- [ ] La tienda aparece «En línea» en el panel central en ≤ 2 min tras arrancar.
- [ ] Apaga el PC de la tienda: pasa a «Caída» en ≤ 3 latidos (3 min).
- [ ] Para solo el servicio VMSBackend: el latido HTTP indica estado «Caída» con el PC encendido.
- [ ] Conteos de hoy de la tienda coinciden con su panel local.
- [ ] El lunes a las 06:00 se genera el informe semanal y se ve en el panel.

## F2. Seguridad y robustez (comprobaciones de la última revisión)

Estas correcciones están probadas en macOS con simuladores; en Windows y con equipos reales falta
confirmarlas:

- [ ] Con un usuario **no administrador** de Windows: no puede abrir `C:\ProgramData\VMSMultimarca\recordings`,
      `config\users.json`, `config\config.json` ni `logs\` (Acceso denegado). El kiosco sí ve sus muros.
- [ ] Con ese mismo usuario: `Invoke-RestMethod http://127.0.0.1:9997/v3/config/paths/list` → **401** (la API
      interna de MediaMTX no responde sin credenciales).
- [ ] `Restart-Service VMSBackend` con los 4 muros abiertos: los muros **vuelven solos** al vídeo sin pasar por
      `/login` (anota el tiempo: ____ s).
- [ ] Mini PC de tienda Windows con analítica: `status.json` → `db.ok = true` y llegan conteos a PostgreSQL (bucle
      de eventos compatible con psycopg en Windows).
- [ ] Panel central instalado en Windows (`-Components Central`): abre, lista sedes y la tarea «Informe semanal»
      termina con código 0.
- [ ] HTTPS (docs/RED.md): `tls-cert`, reinicio, `https://<IP>:8643` desde otro PC con el certificado importado;
      la cookie lleva `Secure`; los muros siguen por `http://127.0.0.1:8600`.
- [ ] Rol de PostgreSQL por tienda (`python -m vms.db.site_roles create …`): con el DSN de la tienda A, un
      `SELECT * FROM line_counts_minute` solo devuelve filas de A.
- [ ] Noche del cambio de hora (último domingo de octubre): la línea de tiempo muestra las dos horas 02:00-03:00
      seguidas, sin solaparse ni dejar hueco.
- [ ] Un NVR con el RTSP en un puerto que contiene «401» (p. ej. 5401): al reiniciarlo, el VMS **no** lo pausa
      30 minutos (el log no dice «rechaza la contraseña»).
- [ ] `logs\audit.log` registra al ver y descargar una grabación (usuario, IP, cámara, tramo).

## G. Firma

| | Nombre | Fecha | Firma |
|---|---|---|---|
| Técnico instalador | | | |
| Responsable del cliente | | | |
