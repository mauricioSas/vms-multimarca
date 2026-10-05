# `tests/ops/`

**Dueño:** B6 · **Referencia:** CONTRATO §18.

| Archivo | Qué prueba |
|---|---|
| `synthetic.py` | Generador de escenas y alteraciones SINTÉTICAS (formas geométricas; nunca fotos ni personas) |
| `doubles.py` | Dobles mientras B5 no entregue `time_read`/`security_read`: hora ONVIF/ISAPI/CGI con el formato real, servidor RTSP (cuenta los intentos con credenciales) y servidor SMTP |
| `test_health_imaging.py` | Una alteración por causa con su puntuación, 50 fotogramas normales sin falsos positivos, < 20 ms a 640 px, histéresis, puerta de inliers, máscaras |
| `test_clock_forecast.py` | Desfase ±2 h y hora manual por los tres protocolos, SNTP, previsión con disco simulado |
| `test_evidence.py`, `test_evidence_viewer.py` | Paquete que `verify` acepta y que falla con un byte cambiado (también en `visor.html` sin red en Chromium); tramo protegido que sobrevive a la retención y caduca |
| `test_notify.py` | Correo con SMTP simulado, webhook con HMAC comprobado por el receptor, agrupación, horas de silencio |
| `test_diagnose.py` | Cada regla del diagnóstico y exactamente 1 petición con credenciales (cliente ISAPI real contra el mock) |
| `test_security_audit.py` | Esquema de la tabla, Hikvision por fecha de build, Dahua por versión, RTSP anónimo, sin salir a Internet |
| `test_camera_scope.py` | Permisos por cámara en vivo, grabación, descarga, analítica, estado y muros; el kiosco igual que antes |
| `test_health_api.py` | Salud por la API, informe, latido y RGPD de la carpeta `ops/` |
| `test_central_and_counts.py` | «Tiendas con problemas hoy» en la central y CSV de conteos (PostgreSQL de pruebas) |
| `test_ui_b6.py` | Asistente completo, ayuda «?» en cada sección, recorrido Driver.js, nada en los muros |
| `test_review_fixes.py` | Regresiones de la revisión: puntuación sostenida, recuperaciones y avisos de disco y huecos, exportaciones cortadas/caducadas/paginadas y espacio libre, límites de protección, copias huérfanas, descarga con permiso vigente, contraseña rechazada, hora del PC, clave desde el arranque, CSV sin fórmulas |
| `test_driver_integration.py` | B6 con los drivers REALES de B5 contra `tools/mocks`: marcas sin API (Ezviz, Reolink) en hora, diagnóstico, auditoría y salud; firmware y hora con la forma real del driver |
| `test_packaging.py` | Los datos de `vms/ops` están en `package-data` (xfail hasta que el arquitecto aplique la petición de `pyproject.toml`) |
