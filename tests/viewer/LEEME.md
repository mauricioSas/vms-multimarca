# `tests/viewer/`

**Dueño:** B2. **Referencia:** PLAN-V2 §6.2 B2.

| Archivo | Qué prueba | Dónde corre |
|---|---|---|
| `test_local_routes.py` | `GET /api/local/kiosk` y `POST /api/local/kiosk-session`: cookie de kiosco firmada, solo desde 127.0.0.1/::1, límite de intentos, `next` solo `/wall/1-4`, CSRF, CSP de la página y cookie válida tras reiniciar el backend | pytest (sin navegador) |
| `test_ui_pages.py` | Las páginas locales del visor (`native/viewer/ui/`) en Chromium con el IPC de Tauri simulado: lo que pintan, los comandos que llaman y con qué argumentos, sin errores ni CSP rota. Con `VMS_TEST_SCREENSHOTS=<carpeta>` guarda capturas | pytest + Chromium de Playwright |
| `smoke_windows.py` | Prueba de humo del `VMS.exe` de prueba por CDP: muro con fotogramas entrando con `kiosk.token`, IPC solo para páginas locales, «sin permiso» con una ACL que niega la lectura y S5 (huella fijada, redirección, reconexión y certificado cambiado) | `python -m tests.viewer.smoke_windows --exe … --out …` en Windows (job `b2-visor` de CI) |

Las pruebas Rust del visor (asignación de monitores, huella TLS, kiosco, `viewer.json`, capacidades IPC) están en
`native/viewer/src-tauri` (`cargo test`). La reconexión de los muros con vídeo real está en
`tests/web/test_wall_reconnect.py` y el aviso de versión nueva en `tests/web/test_wall_update.py`.
