# Resolución de problemas

Antes de nada, mira el registro: `C:\ProgramData\VMSMultimarca\logs\vms.log` (Windows) o
`journalctl -u vms -n 200` (Linux). Los registros nunca muestran contraseñas (aparecen como
`***`). El panel **Estado** del VMS muestra, por cámara, si hay vídeo y el último error.

## «401 Unauthorized» al probar o ver una cámara

- Usuario o contraseña incorrectos, o el usuario no tiene permiso de vista en directo para ese
  canal. Prueba la URL en VLC ([ALTA-EQUIPOS.md](ALTA-EQUIPOS.md)).
- **Usuario bloqueado**: Hikvision y Dahua bloquean el usuario tras varios fallos (de 5 a 30
  minutos). Espera, o desbloquéalo desde la web del equipo con el administrador.
- Autenticación RTSP en «basic» solamente: ponla en **digest** (o digest/basic).
- Hikvision con «Illegal login lock» activo: revisa *Sistema → Seguridad → Servicio de seguridad*.
- Usuario ONVIF distinto del usuario normal (Hikvision): para el alta ONVIF hace falta el usuario
  creado en *Protocolo de integración*.

## «454 Session Not Found» / «404 Not Found» / «Stream not found»

- La ruta o el canal no existen: en un NVR, el canal 1 del VMS es `/Streaming/Channels/101`
  (Hikvision) o `channel=1` (Dahua); si la cámara está en el puerto PoE 5, es el canal 5.
- Cámara IP desconectada del NVR (el NVR responde pero no tiene vídeo de ese canal).
- Algunos Dahua antiguos devuelven 454 si reciben dos `SETUP` muy seguidos: actualiza el firmware.
- Cámara con el subflujo desactivado: actívalo o marca la cámara «sin subflujo» en el VMS.

## Imagen gris, verde o con bloques

- **H.264+/H.265+/Smart Codec** activados: desactívalos (cambian el GOP sobre la marcha).
- Pérdida de paquetes: el VMS usa RTSP por **TCP** por defecto; si cambiaste la cámara a UDP,
  vuelve a TCP. Revisa cables y el puerto del switch (errores CRC).
- Intervalo de I-frame muy largo (p. ej. 200): la imagen tarda en «limpiarse». Ponlo en 1–2× fps.
- Ancho de banda del NVR superado (demasiados flujos principales a la vez): revisa el límite de
  salida del NVR o conecta las cámaras directamente.

## El navegador no muestra vídeo de una cámara (otras sí): H.265

Los navegadores no reproducen **H.265/HEVC** por WebRTC (Chrome y Edge solo en algunos equipos y
versiones). El VMS muestra el aviso «El subflujo es H.265…». Solución: **subflujo en H.264** en
el NVR o la cámara (el flujo principal puede seguir en H.265 y se graba igual).

## La vista en vivo funciona en el PC pero no desde otro equipo o por la VPN

- Puerto **8189/udp** cerrado: el vídeo WebRTC va directo a ese puerto, no por la web (8600).
  Comprueba la regla del firewall y que la red sea «Privada».
- Por la VPN: añade la IP de la VPN del PC a `VMS_MTX_WEBRTC_ADDITIONAL_HOSTS` y reinicia el
  servicio. Si la red bloquea UDP, se usa 8189/tcp (también debe estar abierto).

## Retraso en la vista en vivo

- Normal con WebRTC: 0,3–1 s. Si es mayor:
  - GOP largo en el subflujo (el muro espera a un I-frame): baja el intervalo de I-frame.
  - CPU o GPU al 100 % en el PC de control: reduce cuadrículas de 16 o usa una GPU con
    decodificación por hardware (Edge: `edge://gpu` → «Video Decode: Hardware accelerated»).
  - Wi-Fi o red saturada entre el PC y las cámaras.
- El retraso de la **grabación** no importa: se graba con la hora de llegada.

## Un muro del kiosco aparece en el monitor equivocado

- Orden de pantallas: el muro 1 va en la pantalla más a la izquierda según la configuración de
  pantalla de Windows (no según el número de Windows). Recoloca los monitores en *Configuración →
  Pantalla*.
- Escalado distinto entre monitores (100 % / 150 %): ponlos todos igual. El lanzador intenta
  mover la ventana a su sitio y lo anota en `%LOCALAPPDATA%\VMSMultimarca\kiosk.log`.
- Para probar: `start-kiosk.ps1 -Stop` y luego `start-kiosk.ps1 -Browser Chrome`.

## El muro pide usuario y contraseña

La sesión de kiosco caducó (se renueva sola cada 6 h; caduca a las `VMS_SESSION_HOURS`). Si
cambiaste `VMS_SESSION_HOURS` a menos de 6, arranca el kiosco con `-RefreshHours` menor. Si
cambiaste `VMS_KIOSK_TOKEN`, vuelve a ejecutar `install-kiosk.ps1`.

## El servicio no arranca

- `Get-Content C:\ProgramData\VMSMultimarca\logs\service\VMSBackend.err.log -Tail 50`
- «Puerto en uso»: otro programa usa 8600, 8554, 8889 o 9997 (otro VMS, otro MediaMTX). Cámbialo
  en `.env` o para el otro programa: `Get-NetTCPConnection -LocalPort 9997`.
- Tras editar `.env` a mano, comprueba que no se haya guardado con otra codificación (UTF-8).

## La tienda aparece «Caída» en el panel central

1. ¿Está encendido el PC y conectada la VPN? (`tailscale status`)
2. Latido directo: revisa `VMS_PG_DSN` y que la base acepte la IP de la VPN de la tienda.
3. Agente HTTP: `python -m central.agent --once` en la tienda: «401/403» = token o
   `VMS_SITE_ID` incorrectos; «sin conexión» = VPN o firewall.
4. Hora del PC de la tienda: no influye (la hora del latido la pone el servidor).

## La analítica cuenta mal

- Cámara de puerta **cenital** y a 2,5–4 m; la línea debe cruzar todo el paso, lejos del borde.
- Subflujo a 12–15 fps para la puerta (con menos, el seguimiento pierde a la gente).
- Contraluz en la puerta: activa WDR en la cámara.
- Comprueba `analytics/status.json` (fps procesados y tiempo de inferencia por cámara).
