# Informe: compatibilidad multimarca

> Informe de investigación para la v2 (5 de octubre de 2026). Se conserva tal como se entregó al
> arquitecto. Las decisiones finales están en [`../PLAN-V2.md`](../PLAN-V2.md).

## Compatibilidad multimarca: tabla por marca, plan de pruebas y compra para el laboratorio (5-oct-2026)

**Antes de nada, encontré un fallo en el código actual.** No lo he modificado. `vms/vendors/rtsp_probe.py` (líneas 48-76 y 126) siempre calcula Digest con `algorithm=MD5` y usa el primer reto Digest que recibe. Según la [doc de Frigate](https://github.com/blakeblackshear/frigate/blob/dev/docs/docs/configuration/camera_specific.md), algunos Hikvision recientes traen el Digest en SHA-256 y hay que bajarlo a MD5 a mano. Con un equipo así, la prueba de conexión dice «contraseña incorrecta» aunque sea buena, y además suma un intento fallido al bloqueo (5 intentos / 30 min en Hik y Dahua). El arreglo es implementar SHA-256 (RFC 7616) y elegir el reto más fuerte que se soporte. httpx (ISAPI/CGI) ya trae SHA-256. Para gortsplib/MediaMTX el soporte aparece en el código (`pkg/auth`, `pkg/headers`), pero no lo he probado en ejecución.

### 1. Prioridad por marca (sin cuota de mercado verificada; es una estimación cualitativa para integradores en España)
- **P1 (imprescindible):** Hikvision (con HiLook, HiWatch y Ezviz) y Dahua (con Imou y OEM como Amcrest/Lorex). Son el grueso de NVR, DVR y XVR en retail.
- **P2:** Uniview, TP-Link VIGI, Hanwha, Axis, Ajax (fuerte entre instaladores de alarmas, como Covert) y Milesight.
- **P3 (vía ONVIF genérico y probarlas cuando aparezcan):** Bosch, Reolink, Tapo, Vivotek, i-PRO, Avigilon, UniFi, Grandstream, Tiandy y KEDACOM.

### 2. Tabla por marca

| Marca | RTSP principal / subflujo / canal de NVR | API y puertos | Peculiaridades |
|---|---|---|---|
| **Hikvision** | `/Streaming/Channels/101` principal, `102` subflujo, `103` tercer flujo; canal N del NVR → `N01`/`N02` ([Frigate](https://github.com/blakeblackshear/frigate/blob/dev/docs/docs/configuration/camera_specific.md)). Puerto 554 | ISAPI por HTTP/HTTPS con Digest ([doc ISAPI](https://tpp.hikvision.com/download/ISAPI_OTAP)) | Desde el firmware 5.5 ONVIF viene **desactivado** y usa **usuarios propios**, distintos de los del equipo ([fuente](https://securitycamcenter.com/enable-onvif-hikvision-cameras/)). Bloqueo «Illegal Login Lock»: 5 intentos / 30 min, con 3-5 según modelo ([fuente](https://learncctv.com/hikvision-device-is-locked/)). En DVR/HVR híbridos la numeración de los canales IP varía (p. ej. 33): usar siempre el id que devuelve ISAPI (no verificado con hardware). H.265+/H.264+ funcionan como H.265/H.264 estándar, pero con un GOP muy largo que retrasa el arranque y los cortes de segmento |
| **Ezviz** | `rtsp://admin:<código de verificación>@ip:554/ch1/main` o `/h264/ch1/main/av_stream` | Cloud + SADP | RTSP **apagado** de fábrica: se activa en la app. La contraseña es el código de la etiqueta. Con el cifrado de vídeo activo, su contraseña pasa a ser la de RTSP/ONVIF ([fuente](https://www.visioforge.com/help/docs/dotnet/camera-brands/ezviz/), no verificado en un equipo) |
| **Dahua** (+Amcrest/Lorex OEM) | `/cam/realmonitor?channel=N&subtype=0` principal, `1` subflujo, `2`/`3` flujos extra; añadir `&unicast=true&proto=Onvif` mejora la compatibilidad ([go2rtc](https://github.com/AlexxIT/go2rtc/blob/master/internal/rtsp/README.md)) | CGI `/cgi-bin/*.cgi` y RPC2 por HTTP Digest. TCP 37777 propietario | Bloqueo: 5 intentos / 30 min ([fuente](https://learncctv.com/dahua-account-has-been-locked/)). En XVR los canales IP van detrás de los analógicos (no verificado) |
| **Imou** | Ruta Dahua con el «safety code» como contraseña (no verificado) | Cloud | Igual que Ezviz: depende de la cuenta cloud |
| **Uniview** | `/unicast/c<canal>/s0/live` principal, `s1` subflujo. Puerto 554 (a veces 9090) ([PDF de Uniview](https://www.uniview.com/res/202310/26/20231026_1890323_How%20to%20Get%20the%20URLs%20for%20Uniview%20IPC%20and%20NVR_975509_168459_0.pdf)) | LAPI: no encontré doc pública (no verificado) | En la práctica, ONVIF + RTSP |
| **TP-Link VIGI** | `/stream1` principal, `/stream2` subflujo | ONVIF en el puerto 80 (2020 en firmware antiguo) | Desactivar «Smart Coding» y poner H.264, o la grabación se corrompe al reproducirla ([Frigate](https://github.com/blakeblackshear/frigate/blob/dev/docs/docs/configuration/camera_specific.md)) |
| **Tapo** | `/stream1` principal, `/stream2` subflujo | ONVIF en el puerto 2020 | Hay que crear una «cuenta de cámara» en la app. Las de batería no tienen RTSP ([TP-Link](https://www.tapo.com/us/faq/724/)) |
| **Hanwha** | `/profile<N>/media.smp`; en multisensor `/<sensor>/profileN/media.smp` ([Hanwha](https://support.hanwhavision.eu/hc/en-gb/articles/4414178841746-RTSP-URLs)) | SUNAPI (no verificado) | ONVIF S/G/T |
| **Axis** | `/axis-media/media.amp?camera=1&videocodec=h264` | VAPIX ([lib axis](https://github.com/Kane610/axis), MIT) | Ofrece RTSP por WebSocket |
| **Milesight** | `/main`, `/sub` y `/third`; en multisensor `/sensorN/main` ([Milesight](https://support.milesight.com/support/solutions/articles/69000859092-rtsp-stream-of-milesight-network-camera-nvr-vms)) | — | Hay fuentes que piden doble barra (`//main`). Hay que probarlo |
| **Ajax** | RTSP en el puerto **8554**; las URL se copian desde la app | ONVIF S/G sin pasar por Ajax Cloud ([Ajax](https://support.ajax.systems/en/manuals/onvif/)) | Muy presente entre instaladores de alarmas |
| **Reolink** | `/Preview_01_main` / `_sub`. En NVR conviene más HTTP-FLV `channelN_main.bcs` | API `/api.cgi` ([reolink_aio](https://github.com/starkillerOG/reolink_aio), MIT) | En firmware nuevo, RTSP/ONVIF/HTTP vienen **desactivados** ([Reolink](https://community.reolink.com/topic/3183/)). Su RTSP es inestable según go2rtc |
| **Bosch** | `/?inst=1` principal, `inst=2` segundo flujo, `&line=N` cámara del encoder ([Keenfinity](https://knowledge.keenfinity-group.com/video-systems/article/how-is-rtsp-usage-supported-with-bosch-vip-devices)) | RCP+ | — |
| **UniFi** | `rtsps://nvr:7441/<token>` (con go2rtc, `rtspx` y sin `?enableSrtp`) | [uiprotect](https://github.com/uilibs/uiprotect) (MIT) | Las G5 y posteriores solo dan RTSPS a través de un servidor Protect |
| **Vivotek, i-PRO, Avigilon, Grandstream, Tiandy, KEDACOM** | Usar ONVIF `GetStreamUri` (Vivotek usa `/live.sdp`, sin verificar en equipo) | — | Sin preset propio hasta probarlas con hardware |

### 3. Fuentes reutilizables (licencia leída del repo, estado a hoy)

| Proyecto | Licencia | Último release / actividad | Qué se puede usar |
|---|---|---|---|
| [go2rtc](https://github.com/AlexxIT/go2rtc) | MIT | v1.9.14 (19-ene-2026), push 6-sep-2026 | Rutas y quirks por marca, protocolos Tapo/VIGI/Kasa, **servidor ONVIF para pruebas** |
| [Frigate](https://github.com/blakeblackshear/frigate) | MIT | v0.18.0 (12-sep-2026) | `camera_specific.md` |
| [rroller/dahua](https://github.com/rroller/dahua) | MIT | 1.0.1 (3-oct-2026) | `discovery.py`: descubrimiento Dahua por UDP 37810 **probado contra un NVR real**. Reutilizable con atribución |
| [reolink_aio](https://github.com/starkillerOG/reolink_aio) | MIT | 0.21.17 (17-sep-2026) | API Reolink |
| [pytapo](https://github.com/JurajNyiri/pytapo) | MIT | 3.4.25 (28-sep-2026) | API Tapo |
| [pyHik](https://github.com/mezz64/pyHik) | MIT | sin releases, push 24-sep-2026 | Eventos ISAPI |
| [axis](https://github.com/Kane610/axis) | MIT | v74 (5-jul-2026) | VAPIX |
| [uiprotect](https://github.com/uilibs/uiprotect) | MIT | v23.0.0 (4-oct-2026) | API Protect y sus fixtures JSON |
| [MediaMTX](https://github.com/bluenviron/mediamtx) / [gortsplib](https://github.com/bluenviron/gortsplib) | MIT | v1.21.1 (20-sep-2026) / tag v5.6.6 | Motor del producto y base para el simulador |
| [daniela-hase/onvif-server](https://github.com/daniela-hase/onvif-server) | MIT | sin releases, push 31-may-2026 | Emulador ONVIF Profile S en Node |
| [Home Assistant core](https://github.com/home-assistant/core) | Apache-2.0 | push 4-oct-2026 | Código de las integraciones (mirar la licencia de cada dependencia por separado) |

**Qué evitar:**
- **python-amcrest es GPL-2.0.** Lo usa la integración de Amcrest de Home Assistant.
- **WSDiscovery es LGPL-3.0.** Lo usa la integración ONVIF de Home Assistant. Nosotros ya lo tenemos implementado por nuestra cuenta en `vms/vendors/discovery.py`, que es lo correcto.
- **onvif_srvd (GPL-2) y onvif_simple_server (GPL-3):** solo como herramientas de laboratorio externas, nunca dentro del repo ni del instalador.
- **kate-goldenring/onvif-camera-mocking no tiene licencia:** no se puede usar.
- **La base de URLs de iSpy** tiene copyright y el repo marca la licencia como NOASSERTION: no copiarla ni scrapearla.
- **Fixtures:** pyHik, reolink_aio, axis y pytapo no traen fixtures de respuestas reales. Hay que grabar las nuestras.

### 4. Cómo probar sin tener todo el hardware
1. **Fixtures propias.** Una herramienta que guarde las respuestas reales de ISAPI, CGI, ONVIF y el SDP, anonimizando serie, MAC e IP, y tests de los parsers contra ellas. Se pueden pedir capturas a Covert.
2. **MediaMTX + ffmpeg como herramienta de laboratorio** (no se distribuye):
   - Códecs: H.264 baseline/high, H.265, MJPEG.
   - Audio: PCMA, AAC y sin audio.
   - GOP de 10 s para simular H.265+/Smart Codec.
   - Un solo perfil.
   - Autenticación con `rtspAuthMethods: [basic]` o `[digest]`.
   - Transporte con `rtspTransports` solo TCP o solo UDP.
3. **Un servidor RTSP «caótico» propio** (asyncio, en `tools/mocks`) para lo que MediaMTX no hace:
   - Digest SHA-256 y varios `WWW-Authenticate` en la misma respuesta.
   - SDP raros: sin `a=control`, control absoluto, payload dinámico sin `rtpmap`, sin `sprop-parameter-sets`/VPS, saltos de línea LF.
   - Rutas con doble barra.
   - Rechazo con 453/503 a partir de N sesiones.
   - Corte de la conexión si no hay keepalive.
4. **ONVIF:** el servidor ONVIF de go2rtc más nuestros mocks. Casos a cubrir:
   - Un solo perfil.
   - Desfase de reloj del equipo, porque el UsernameToken depende de la hora.
   - ONVIF desactivado.
   - Usuario ONVIF distinto del usuario del equipo.
5. **Bloqueo de cuentas.** Un test que asegure como mucho 1 intento fallido por alta, y que una cuenta bloqueada se distinga de una contraseña mala.
6. **Hardware real:**
   - Prueba de 72 h con reconexión tras reinicio y cambio de IP.
   - Matriz publicada de marca × modelo × firmware × (descubrir, alta, directo principal/subflujo, grabar, reproducir, snapshot).

### 5. Descubrimiento (todo se puede hacer con licencia limpia)
- **WS-Discovery** (UDP 3702 multicast): ya está hecho por nosotros.
- **SADP de Hikvision** (UDP multicast 239.255.255.250:37020, Probe en XML): implementarlo desde cero y **solo la parte de búsqueda**. SADP también permite activar equipos y resetear contraseñas, y eso no hay que tocarlo.
- **Dahua** (UDP 37810, cabecera DHIP de 32 bytes + JSON `DHDiscover.search`): adaptar `discovery.py` de rroller/dahua (MIT). Ese puerto se ha usado para ataques de amplificación: limitar el envío a la LAN y a pocos paquetes por segundo.
- **SSDP/UPnP y mDNS** (Axis y UniFi): implementación propia o `pkg/mdns` de go2rtc (MIT).

### 6. Lista de compra para el laboratorio
Los modelos son orientativos; precio y disponibilidad no están verificados.
1. Cámara Hikvision serie 2 con firmware nuevo (DS-2CD2043G2-I).
2. Cámara HiLook o Ezviz (C6N).
3. NVR Hikvision de 4 canales PoE.
4. DVR Turbo HD híbrido de 4 canales con 1 cámara analógica.
5. Cámara Dahua (IPC-HFW2441S).
6. NVR Dahua 4KS3.
7. XVR Dahua de 4 canales.
8. Cámara Imou.
9. Cámara Uniview IPC2122.
10. TP-Link VIGI C440 y Tapo C210.
11. Reolink RLC-510A.
12. Una cámara Ajax: pedirla prestada a Covert.
13. Una Axis M10 y una Hanwha serie Q: prestadas o de segunda mano.

Con P1 y P2 completos (puntos 1-11, sin Axis/Hanwha/Ajax) se cubre la mayoría de los casos reales de retail (estimación, no verificado).
