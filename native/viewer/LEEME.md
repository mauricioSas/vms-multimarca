# `native/viewer/` — visor de escritorio `VMS.exe`

**Dueño:** B2. **Referencias:** PLAN-V2 §1.1, §2.3 y §2.5; CONTRATO §17. **Plan B:** [Electron](PLAN-B-ELECTRON.md)
(solo si la prueba S1 dice que WebView2 no decodifica 4×16 flujos por hardware).

El visor es una carcasa **Tauri 2.12.1** (WebView2 en Windows). No contiene el backend: muestra la interfaz que sirve
el servicio (`/`, `/wall/N`, `/status`…) del propio PC (`http://127.0.0.1:8600`) o de otro (`https://host:8643`), y
solo tiene páginas propias para lo que pasa antes de conectar (`ui/`). Así la interfaz y el backend siempre tienen la
misma versión.

## Qué hace

| Función | Cómo |
|---|---|
| Bandeja | Icono con el color de `GET /api/health` del servidor del panel (verde, ámbar, rojo; gris al arrancar). Menú: Abrir panel · Muros (mostrar u ocultar todos, Muro 1-4, Reasignar monitores…) · Estado del sistema · Actualizaciones (estado, Buscar ahora, Volver a la versión anterior…) · Servidores… · Abrir al iniciar sesión · Acerca de · Salir (la grabación sigue). Clic izquierdo = panel |
| Panel | Ventana normal con la interfaz completa. Cerrarla la oculta (el visor sigue en la bandeja) |
| Muros por monitor | `VMS.exe --walls` o la bandeja. Una ventana sin bordes a pantalla completa por muro. Clave del monitor `nombre\|x,y\|anchoxalto` en píxeles físicos (no cambia con el escalado de Windows). Cada 5 s se miran los monitores: si uno desaparece, su muro se oculta (y a los 60 s oculto para el vídeo); si vuelve, reaparece. Los demás muros nunca se mueven. Tabla de casos en `src/monitors.rs` |
| Instancia única | `tauri-plugin-single-instance` 2.5.2: un segundo `VMS.exe [--walls] [--abrir …]` pasa sus argumentos al primero |
| Inicio con la sesión | «Abrir al iniciar sesión» escribe `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\VMS Multimarca` = `"<raíz>\bin\vmshost.exe" viewer --bandeja` (la ruta del visor cambia con cada versión; el arrancador fijo no). Los muros del puesto de control al iniciar sesión los configura el instalador (`HKLM\…\Run` con `--walls`) |
| Muros sin contraseña | Ver «Entrada de los muros» |
| Lista de servidores con huella | Ver «Servidores remotos y certificado» |
| «Conectando…» y diagnóstico | Si el servicio no responde, la ventana muestra «Conectando con el servicio…» y reintenta cada 2 s; «Ver diagnóstico» enseña el estado del servicio, de la clave de los muros y de `updater\public-status.json` |
| Aviso de actualización | Cada 10 s lee `<datos>\updater\public-status.json` (CONTRATO §15.6). Si la versión instalada ya no es la suya y su `VMS.exe` es distinto (si solo cambió la aplicación web, es el mismo archivo y no se reinicia), se reinicia desde `versions\<instalada>\viewer\VMS.exe`: de inmediato si hay muros abiertos o nadie usa el panel; si alguien está usando el panel, le muestra el aviso «Hay una versión nueva del visor…» y espera a 60 s sin actividad |
| Versión nueva desde GitHub | `github_update.rs`: al minuto y medio de arrancar y cada 6 h consulta las versiones publicadas en GitHub Releases (con `curl.exe` de Windows: certificados del sistema). Si hay una más nueva que la instalada (una beta recibe betas; una final, solo finales), la bandeja dice «Versión nueva X: descargar e instalar…» y, si se está usando el panel, se abre «Actualizaciones» una vez. «Descargar e instalar» baja el instalador, comprueba su SHA-256 con el `SHA256SUMS.txt` de esa versión y lo lanza con elevación (UAC) en modo `/SILENT` con el tipo de puesto instalado: se instala encima y se conservan configuración y grabaciones. Solo descarga de `github.com/mauricioSas/vms-multimarca/releases/download/` |
| Vuelta atrás | «Volver a la versión anterior…» abre `actualizaciones.html` y lanza `vmsctl update rollback --json --reason "…"` **con elevación** (UAC). Sin credenciales de administrador no se puede. «Buscar ahora» hace lo mismo con `vmsctl update check` |

## Entrada de los muros (CONTRATO §17.1)

1. El visor lee `ProgramData\VMSMultimarca\secrets\kiosk.token` (ACL: SYSTEM, Administradores, el servicio y el grupo
   `VMS Operadores`). Admite el token en texto o un blob DPAPI de máquina (se reconoce por su cabecera).
2. Lleva la ventana del muro a `GET /api/local/kiosk` del backend local (una página mínima, sin scripts).
3. Al terminar de cargar, ejecuta en esa página un `fetch` a `POST /api/local/kiosk-session`
   (`{"token", "next": "/wall/N"}`, cabecera `X-Requested-With: vms`). La respuesta 204 deja la cookie de kiosco
   firmada en el almacén del WebView y el script abre `/wall/N`.

El token no pasa nunca por la línea de órdenes, la URL, el registro, las páginas locales ni el IPC. Las dos rutas solo
responden desde 127.0.0.1/::1. Si el usuario de Windows no puede leer el archivo, el muro muestra **«Sin permiso para
abrir los muros»** y lo vuelve a intentar cada 30 s. Si un muro vuelve a `/login` (cookie caducada a los 30 días o token
rotado con `vmsctl kiosk rotate`), el visor repite el intercambio; si el servidor lo rechaza otra vez, muestra el
motivo en vez de un bucle.

## Servidores remotos y certificado (S5)

`viewer.json` (CONTRATO §17.2) guarda la lista de servidores con la huella SHA-256 de su certificado. `http://` solo se
admite para el propio PC; un servidor de otro equipo va siempre por `https://` y con huella.

- **Alta («Servidores…»):** «Probar» abre una conexión TLS propia (rustls) y enseña la huella del certificado que
  presenta el servidor; la persona la compara con la del servidor y la guarda. La conexión TLS comprueba la firma del
  intercambio con la clave del certificado, así que nadie sin la clave privada puede presentar el certificado fijado.
- **Antes de abrir cada ventana** contra ese servidor se vuelve a leer la huella. Si cambió, la ventana **no carga
  nada** y muestra «El certificado del servidor cambió» con las dos huellas; para confiar en la nueva hay que escribir
  sus 4 últimos caracteres.
- **Dentro de WebView2 (opción A de S5):** el certificado autofirmado del servidor se acepta en el evento
  `ServerCertificateErrorDetected` solo si su huella es la guardada para ese origen; cualquier otro se cancela y la
  ventana pasa al aviso. WebView2 guarda la decisión por host y certificado, así que un certificado nuevo vuelve a pasar
  por la comprobación (también tras una redirección del mismo servidor, WebView2Feedback #4575).
- **Límite conocido:** si el certificado del servidor es de confianza para Windows (raíz importada con
  `vmsctl tls setup --import-root`), WebView2 no lanza ese evento y la fijación depende solo de la comprobación previa
  de cada ventana (y del GET de salud de la bandeja, que también va con la huella exigida).

La prueba de humo de Windows (`tests/viewer/smoke_windows.py`) lo ejecuta de verdad: carga con la huella fijada
(también tras un 302), reconexión tras reiniciar el servidor y aviso sin cargar nada con otro certificado, abierto o al
arrancar.

## Seguridad del contenedor

- **Sin IPC para páginas remotas.** Una sola capacidad, `capabilities/local.json`, con `"local": true` y sin clave
  `remote`: solo las páginas de `ui/` (`tauri://localhost` en macOS, `http://tauri.localhost` en Windows) pueden
  llamar a los comandos del visor (los 16 de `src/commands.rs`, declarados en `build.rs`), y ni siquiera ellas tienen
  los plugins del núcleo (`core:*`). Lo prueban `tests/ipc_acl.rs` (con el runtime simulado y las capacidades reales)
  y la prueba de humo (desde la página real del backend). `tauri.conf.json` no tiene `devUrl` (lo haría «local»).
- **Navegación limitada** a las páginas locales y a los orígenes de los servidores configurados. Una página del backend
  no puede abrir una página local por su cuenta (solo el visor lleva una ventana a `ui/`), ni ventanas nuevas.
- **Comandos que actúan sobre la ventana que llama**, nunca sobre lo que diga la URL de la página.
- **CSP** estricta en las páginas locales (`script-src 'self'`, sin estilos ni scripts en línea).
- **Sin herramientas de desarrollo** en la versión publicada: no se compila la característica `devtools`, y al arrancar
  se borran las variables `WEBVIEW2_*` (nadie puede abrir el puerto de depuración con una variable de entorno). Solo
  la build de prueba (`--features prueba`) abre CDP con `VMS_VIEWER_CDP_PORT`.
- En los muros se quita el menú contextual de WebView2.

## Compilar y probar

Toolchain fijado en `src-tauri/rust-toolchain.toml` (Rust 1.99.0). Espacio de trabajo propio (no entra en el
`cargo test --workspace` de `native/`); usa `vms-common` (escritura atómica y ocultación de credenciales) por ruta.

```bash
cd native/viewer/src-tauri
cargo test                         # 62 pruebas: monitores, huella TLS, kiosco, viewer.json, IPC/ACL…
cargo clippy --all-targets -- -D warnings && cargo fmt --check
cargo deny --manifest-path Cargo.toml --config ../../deny.toml check licenses bans sources
npx --yes @tauri-apps/cli@2.12.1 build --no-bundle                   # publicada → target/release/VMS(.exe)
npx --yes @tauri-apps/cli@2.12.1 build --no-bundle --features prueba # de prueba (CDP)
```

Pruebas de Python relacionadas: `tests/viewer/` (rutas `local`, páginas de `ui/` con el IPC simulado) y
`tests/web/test_wall_*.py` (reconexión de los muros con vídeo real, versión nueva). En CI, el job `b2-visor` de
`ci.yml` (windows-latest) hace todo lo anterior y la prueba de humo por CDP.

Probar a mano contra un backend de desarrollo:

```bash
VMS_VIEWER_CONFIG_DIR=/tmp/visor VMS_DATA_DIR=<carpeta de datos del backend> target/debug/VMS --walls
```

## Línea de órdenes

| Argumento | Efecto |
|---|---|
| (ninguno) | abre el panel |
| `--walls` | abre los muros configurados (o uno por monitor, hasta 4) |
| `--panel` | abre el panel (se puede combinar con `--walls`) |
| `--bandeja` | arranca solo en la bandeja (inicio con la sesión) |
| `--abrir <servidores\|monitores\|acerca\|diagnostico\|actualizaciones>` | abre esa página |
| `--after-pid <pid>` | uso interno al reiniciarse en otra versión: espera a que termine el visor anterior |

Variables: `VMS_DATA_DIR` (como el backend), `VMS_VIEWER_CONFIG_DIR` (pruebas: carpeta de `viewer.json`) y, solo en la
build de prueba, `VMS_VIEWER_CDP_PORT`.

## Archivos

| Ruta | Qué es |
|---|---|
| `src-tauri/src/app.rs` | ventanas, bandeja, hilos (salud 5 s, monitores 5 s, actualizaciones 10 s), arranque |
| `src-tauri/src/commands.rs` | comandos IPC de las páginas locales |
| `src-tauri/src/monitors.rs` | asignación de muros a monitores (función pura con su tabla de casos) |
| `src-tauri/src/pinning.rs`, `http.rs` | huella TLS (rustls + ring) y GET de salud con la huella exigida |
| `src-tauri/src/webview2_cert.rs` | manejador `ServerCertificateErrorDetected` (solo Windows) |
| `src-tauri/src/kiosk.rs` | lectura de `kiosk.token` y script del intercambio |
| `src-tauri/src/config.rs`, `state.rs`, `servers.rs` | `viewer.json`, estado compartido y reglas de URL y navegación |
| `src-tauri/src/updates.rs`, `platform.rs` | estado de actualizaciones; UAC, registro, inactividad y DPAPI de Windows |
| `ui/` | páginas locales: conectando, diagnóstico, servidores, certificado, sin permiso, monitores, actualizaciones, acerca de |
| `tools/make_icons.py` | genera `src-tauri/icons/` (los iconos de la bandeja se dibujan en `src/tray_icons.rs`) |

Registro del visor: `%APPDATA%\VMSMultimarca\visor.log` (1 MB + `.1`), con las credenciales ocultadas.

## Lo que no está verificado

- **S1** (decodificación por hardware con 4 × 16 flujos) sigue pendiente del PC del laboratorio. Si falla, plan B:
  [Electron](PLAN-B-ELECTRON.md).
- El tamaño del grupo de conexiones de WebView2 con 4 muros de 16 se ha cuidado (una sola conexión SSE para todos los
  muros, `vms/web/static/js/wall-events.js`) pero no se ha medido con 4 monitores reales.
- Muros en varios monitores reales, DPI mezclado, desconexión física de un monitor y el reinicio con la sesión de la
  cuenta de muros: se prueban en el laboratorio (el runner de CI tiene un solo monitor virtual).
- La vuelta atrás desde la bandeja depende de `vmsctl update rollback` (B1/B4); el visor solo lo lanza con elevación.
