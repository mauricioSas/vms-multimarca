# `tests/compat/`

**Dueño:** B5. Matriz de integración con simuladores (PLAN-V2 §4.2), parte de drivers.

```
pytest tests/compat                       # necesita MediaMTX (bin/ o VMS_MEDIAMTX_BIN) y ffmpeg (VMS_TEST_FFMPEG o PATH)
pytest tests/vendors                      # mocks, servidor caótico y batería de contrato (sin binarios)
```

Herramientas de laboratorio, **nunca en el producto**: MediaMTX como «cámara», ffmpeg de pruebas, el servidor
RTSP caótico (`tools/mocks/rtsp_chaos.py`), los mocks ISAPI/CGI/ONVIF y los respondedores WSD/SADP/DHIP.
MediaMTX no admite «:» ni «/» en las contraseñas de sus usuarios: el laboratorio usa `Lab#Pass!2@x`; la
contraseña con todos los caracteres conflictivos (`Sim#Pass:1@/x`) se prueba con el servidor caótico y camsim.

## Estado de cada caso

| Caso de §4.2 | Dónde | Qué se comprueba aquí | Lo que falta (de otro bloque) |
|---|---|---|---|
| H.264 baseline / high con B-frames | `test_matrix.py::test_codecs_and_audio_through_mediamtx` | códec del SDP y primer fotograma clave < 6 s | grabar, `/list`, muro WebRTC (B1/B2) |
| H.265 principal + H.264 subflujo | `test_h265_main_h264_sub_profile` | alta correcta y aviso de H.265 | pantalla completa con etiqueta y «Descargar» (B2) |
| H.265 en el subflujo | `test_h265_sub_offers_codec_fix_and_mock_accepts_put` | aviso + «Corregir códec» (el mock acepta el PUT) | — |
| MJPEG por RTSP | `test_mjpeg_is_flagged_for_the_wall` | MediaMTX lo acepta; aviso «no compatible con WebRTC» | marca en el muro (B2) |
| Audio PCMA / AAC / sin audio | `test_codecs_and_audio_through_mediamtx` | pistas de audio en el SDP sin romper la prueba | grabar con audio (B1) |
| GOP 10 s (H.265+) | `test_long_gop_first_frame_under_12s` y `tests/vendors/test_rtsp_chaos.py::test_slow_first_frame_is_measured` | primer fotograma < 12 s; `gop_slow` y aviso si > 4 s | espera de 12 s del muro con `gop_hint` (B2, petición) |
| Digest MD5 / SHA-256 / Basic | `test_basic_only_camera_needs_allow_basic`, `tests/vendors/test_rtsp_chaos.py`, `test_one_upstream_session_for_many_readers[digest-sha256]` | elige SHA-256 > MD5 > Basic; Basic solo con `allow_basic`; **MediaMTX v1.21.1 sí responde a un reto SHA-256** como cliente (comprobado aquí) | — |
| Solo TCP / solo UDP | — | — | `rtspTransport` de la cámara en el motor (B1) |
| NVR multicanal (16 ch, 2 sin vídeo) | `test_nvr_16_channels_import_and_sources`, `tests/vendors/test_vendors_api.py` | 16 canales, estados, 16 rutas con credenciales `%XX` | grabación de los 16 (B1) |
| XVR (4 analógicos + 4 IP desde el 5) | `tests/vendors/test_device_v2.py::test_dahua_xvr_ip_channels_after_analog`, batería de contrato | ids de canal del equipo | — |
| DVR híbrido Hikvision (IP desde el 33) | `test_hybrid_dvr_ids_map_to_rtsp_paths`, `test_device_v2.py` | ids 33-36 → rutas 3301… | — |
| Bloqueo de usuario | `tests/vendors/test_rtsp_chaos.py::test_lockout_is_distinguished_from_bad_password`, `test_device_v2.py::test_locked_user_is_reported_with_minutes` | 1 intento; «bloqueado N min» ≠ contraseña mala | — |
| Límite de sesiones | `test_one_upstream_session_for_many_readers` | MediaMTX abre **una** sesión hacia un NVR de 2 sesiones aunque lean 4 | — |
| Corte y vuelta del equipo | — | — | reconexión del motor (B1) |
| ONVIF: perfil único, reloj ±2 h, ONVIF desactivado, usuario distinto | `tests/vendors/test_capabilities.py::test_onvif_clock_skew_is_corrected_and_read`, batería (Media1 de un perfil) | desfase corregido y medido; mensajes claros | — |
| Descubrimiento WSD / SADP / DHIP | `tests/vendors/test_discovery_v2.py` | marca > 0,7; DHIP limitado a la LAN y a 5 paquetes/s | — |
| ONVIF Media2 / Profile T con H.265 | `tests/vendors/test_onvif_discovery.py`, batería (Profile T) | Media2 preferido; H.265 correcto; Media1 sin error | — |
| NVR al límite (8 sesiones, 503) | `tests/vendors/test_rtsp_chaos.py::test_busy_and_session_limit` | mensaje claro, `session_limit`, sin reintentos | que las que entran graben (B1) |
| Cambio de IP por DHCP | `tests/vendors/test_vendors_api.py::test_identity_and_ip_change` | propuesta por serie/MAC; con «seguir la IP», cambio solo | volver a grabar < 2 min (B1 + bucle periódico, petición al arquitecto) |
| Firmware que desactiva RTSP | `tests/vendors/test_device_v2.py::test_firmware_change_with_rtsp_closed` | «el equipo se actualizó (A → B)…» con los pasos del driver | estado en `/api/status` (integración) |
| «Corregir códec» y deshacer | `tests/vendors/test_capabilities.py`, `test_vendors_api.py` | copia antes del cambio, deshacer exacto, auditoría | — |
