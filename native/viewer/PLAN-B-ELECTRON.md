# Plan B del visor: Electron (si S1 falla)

**Cuándo se activa:** si la prueba **S1** (PLAN-V2 §4.7, `spikes/s1-webview2/`) da que WebView2 **no** decodifica
4 muros × 16 subflujos H.264 por hardware con CPU < 60 % y < 1 % de fotogramas perdidos, y bajar la resolución de los
subflujos no basta. Hasta entonces el visor es Tauri (decisión N4 de la noche del 5/10, PLAN-V2 §9.1).

**Qué cambia y qué no.** El visor es una carcasa: la interfaz la sirve el backend. Pasar a Electron cambia la carcasa,
no la interfaz ni el backend. Todo lo que es contrato se mantiene:

| Pieza | Con Tauri (hoy) | Con Electron (plan B) |
|---|---|---|
| Contenedor | WebView2 Evergreen del sistema (pocos MB) | Chromium propio de Electron 44.x (MIT), ~100 MB más por versión |
| Muros y panel | `WebviewWindow` por monitor | `BrowserWindow` por monitor (`screen.getAllDisplays()`, `setBounds`, `setFullScreen`) |
| Asignación de monitores | `src/monitors.rs` | la misma regla portada a TypeScript, con la **misma tabla de casos** como prueba |
| Bandeja e instancia única | `TrayIconBuilder`, `tauri-plugin-single-instance` | `Tray`, `app.requestSingleInstanceLock()` |
| Páginas locales | `ui/` con IPC solo local (`capabilities/local.json`) | las mismas `ui/`, cargadas con `file://`/protocolo propio; `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`, y un `preload` que expone **solo** los 16 comandos y **solo** si `location.origin` es el del protocolo local |
| Páginas del backend | sin IPC | sin `preload` (ventanas de muro/panel sin puente): no tienen acceso a nada del contenedor |
| Navegación | `on_navigation` + `on_new_window` | `will-navigate`, `setWindowOpenHandler(() => ({action: 'deny'}))` |
| Entrada de los muros | `/api/local/kiosk` + `fetch` ejecutado desde el contenedor | igual, con `webContents.executeJavaScript` (no cambia el backend) |
| Fijación del certificado | evento `ServerCertificateErrorDetected` de WebView2 + comprobación TLS previa | `app.on('certificate-error')` / `session.setCertificateVerifyProc`, comparando `certificate.fingerprint` (SHA-256) con la huella guardada; misma comprobación previa en el proceso principal (`tls.connect`) |
| Reinicio en otra versión | `--after-pid` + `versions\<X>\viewer\VMS.exe` | igual |
| Vuelta atrás con UAC | `ShellExecuteExW("runas", vmsctl …)` | igual, con un módulo nativo mínimo o `powershell Start-Process -Verb RunAs` sin texto del usuario en la orden |
| CDP en pruebas | `--features prueba` + `VMS_VIEWER_CDP_PORT` | `app.commandLine.appendSwitch('remote-debugging-port', …)` solo en la build de prueba |

**Licencias.** Electron es MIT, pero trae Chromium y **FFmpeg** (LGPL-2.1 en la build de Electron, que es la
variante «propietaria» con H.264). Antes de elegirlo hay que verificar que la `ffmpeg.dll` que distribuye Electron es
la LGPL dinámica (no la GPL), incluir el aviso y el ofrecimiento de fuentes en `THIRD_PARTY_NOTICES.txt` y añadir la
excepción a `tests/test_licenses.py` (lo decide el arquitecto: PLAN-V2 §1.1 dice «no verificado»).

**Coste estimado del cambio:** 4-6 días (carcasa + preload + pruebas portadas), sin tocar el backend ni `ui/`.

**Qué se reutiliza tal cual:** `ui/` (páginas y textos), `tests/viewer/test_ui_pages.py` (simula el IPC con
`window.__TAURI_INTERNALS__.invoke`; el preload de Electron expondría la misma función), las rutas `local` del
backend, `wall.js`, `whep.js`, `wall-events.js` y la prueba de humo por CDP (cambiando solo cómo se arranca el visor).

**Criterio para volver a Tauri:** si una versión posterior de WebView2 pasa S1, se vuelve a Tauri (menos peso y
WebView2 lo actualiza Windows).
