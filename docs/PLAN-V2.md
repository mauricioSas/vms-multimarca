# Plan v2 — VMS Multimarca como app de Windows con actualizaciones y compatibilidad multimarca verificada

**Fecha:** 5 de octubre de 2026 · **Versión del plan:** 1.3 (1.1 = revisión crítica, §8; 1.2 = bloque
B6 «Operación, IA de verificación y onboarding» y las decisiones de la noche del 5/10, §9; 1.3 = revisión de
la fase 0: dueños de los archivos compartidos, componente `data` y licencias Rust, §6.2, §2.7 y §9.2) ·
**Producto actual:** 0.1.0 (fases 1-2 construidas, primera instalación real en Windows 11
10.0.26200 el 4-oct-2026) · **Fase 0:** hecha en la rama `v2` (resultado de las pruebas de concepto en
[`investigacion-v2/spikes.md`](investigacion-v2/spikes.md)).

Este documento es la base para construir la v2. Lo que dice cada parte lo respaldan los informes de
investigación de [`docs/investigacion-v2/`](investigacion-v2/): app e instalador, actualizaciones,
cámaras y auditoría del código. Las afirmaciones sobre proyectos de terceros se comprobaron el
5-oct-2026 en GitHub (API y archivo `LICENSE`). Lo que no se pudo confirmar va marcado como
**no verificado**.

Mientras se construye la v2, sigue mandando [`docs/CONTRATO.md`](CONTRATO.md). La fase 0 (§6.1) lo
amplía con las secciones §13-§17 que se describen aquí.

---

## 0. Resumen en una página

| Objetivo del cliente | Respuesta de la v2 |
|---|---|
| **App de Windows de verdad** | Visor de escritorio **Tauri 2** (`VMS.exe`, firmado): ventana propia, icono, menú Inicio, bandeja, una ventana a pantalla completa por monitor. Grabación, motor y analítica siguen como **servicios de Windows**, que funcionan sin nadie conectado. El usuario no ve nunca «abre el navegador en 127.0.0.1». |
| **Instalador con interfaz gráfica** | **Inno Setup 7.1** con asistente en español: tipo de puesto, carpeta de grabaciones con comprobación de disco, sede, HTTPS y conservar grabaciones al desinstalar. En las 147 tiendas se instala en silencio con `/VERYSILENT /LOADINF=`. Sin PowerShell: la configuración del equipo la hace `vmsctl.exe`, una herramienta nuestra en Rust. |
| **Actualizaciones seguras con rollback** | Servicio `VMSUpdater` con **python-tuf 7** (TUF: firmas con umbral, protección contra versiones viejas y metadatos congelados). Versiones en carpetas paralelas; la versión activa la elige un **arrancador fijo** (`vmshost.exe`) leyendo un puntero que se escribe de forma atómica, con un **diario** que sobrevive a un corte de luz. Health check de 120 s y **vuelta atrás automática**, también si el actualizador nuevo no arranca. Repositorio en **Cloudflare R2** con un Worker que pide el token de la sede. Canales `pilot` y `stable` y canal por sede desde el panel central. La clave que autoriza versiones (`targets`) vive en una **llave física** y nunca en CI. |
| **Sin cortar la grabación al actualizar** | MediaMTX pasa a ser **su propio servicio** (`VMSEngine`) y sus rutas viven en su propio archivo de configuración: **graba aunque el backend esté caído**. Al actualizar app o runtime, la grabación no se corta (**0 s**). Al actualizar el motor, **≤ 15 s** por la noche, medido en CI. |
| **Compatibilidad multimarca verificada** | **Registro de drivers** (`vms/vendors/registry.py`): añadir una marca toca un solo sitio. La v2 trae **3 drivers con API** (Hikvision ISAPI, Dahua CGI y ONVIF genérico, con Media2/Profile T), **11 perfiles RTSP/ONVIF por marca** y RTSP manual. Cada uno muestra su **madurez real** en la interfaz: solo es «Verificado» con su prueba de 72 h con hardware. **Hikvision y Dahua tienen que estar verificados con hardware antes del piloto.** Batería de contrato con respuestas grabadas de equipos reales, servidor RTSP «caótico» y matriz de hardware publicada. |
| **Operación y verificación** (bloque B6, §6.2) | Salud de imagen 0-100 con OpenCV clásico (tapada, desenfocada, movida, negra, congelada, IR…), informe de salud por tienda y en la central, desfase horario, previsión de días de grabación, exportación de evidencias firmada con visor portátil, marcadores que la retención no borra, avisos por correo y webhook, «¿por qué no conecta?», auditoría de seguridad de los equipos, permisos por cámara, línea de tiempo con eventos y onboarding con Driver.js. Sin datos personales: la referencia de cada cámara es una mediana de fotogramas. |
| **Calidad muy alta, revisada** | CI en GitHub Actions con **windows-latest**: pytest completo; luego, en Windows real, instalación silenciosa → servicios → visor (por CDP) → actualización v1→v2 con medición del corte → rollback de una v3 rota → desinstalación. En el laboratorio, **cortes de luz simulados** (VM Hyper-V apagada en cada paso de la actualización) y checklist de hardware con 72 h de prueba. |

**Coste fijo aproximado:** 25-60 €/mes más 350-700 € de entrada (certificado Certum OV, licencia de
Inno, 2 llaves YubiKey). El hardware del laboratorio va aparte (§7.3).

**Tiempo:** fase 0 de 5-6 días (con 5 pruebas de concepto). Después, 5 bloques de 3-4 semanas (el
visor, B2, no arranca hasta tener el veredicto de S1), 1 semana de integración y la prueba con
hardware. El calendario lo marca el hardware real, no el código.

---

## 1. Decisiones de stack

Cada decisión lleva su justificación, las alternativas que se descartaron y en qué condición se
cambiaría.

### 1.1 Contenedor de escritorio: **Tauri 2.12.x** (fijado `tauri = "=2.12.1"`)

- **Qué es:** [tauri-apps/tauri](https://github.com/tauri-apps/tauri), Apache-2.0/MIT. La última
  estable es **tauri-v2.12.1 (30-09-2026)**. Existe `tauri-v3.0.0-alpha.4` (01-10-2026), que **no se
  usa** hasta que salga estable.
- **Por qué:**
  - Usa el **WebView2 Evergreen** que ya trae Windows 11. Pesa pocos MB y no distribuye Chromium ni FFmpeg.
  - Tiene ventanas múltiples (`WebviewWindow`, `availableMonitors()`, `setPosition`, `setFullscreen`).
  - Bandeja del sistema en el núcleo (**no verificado** en el código).
  - Plugins oficiales (Apache-2.0) para instancia única y arranque con la sesión.
  - La parte nativa es Rust, el mismo lenguaje que `vmsctl.exe` (§1.3). Es un solo toolchain nuevo para el equipo.
- **Modelo:** el visor **no contiene el backend**. Es una carcasa que muestra la interfaz web que ya
  sirve el backend (`/`, `/wall/N`, `/playback`…), en local (`127.0.0.1:8600`) o en otro PC
  (`https://host:8643`). Las únicas páginas propias del visor son las locales: elegir servidor, error
  de conexión y «Acerca de». Así la interfaz y el backend siempre tienen la misma versión, y no se
  duplica la UI.
- **Descartadas:**
  - **Electron 44.5.1** (MIT): más de 100 MB y su propio Chromium con FFmpeg (licencia LGPL, **no
    verificado** en este repaso). Es el **plan B** si la prueba S1 (§6.1) demuestra que WebView2 no
    decodifica 4×16 flujos por hardware.
  - **pywebview 6.2.1** (BSD-3): sin bandeja integrada y con menos control de ventanas por monitor.
  - **WPF + WebView2:** obligaría a añadir .NET.
  - **Neutralinojs 6.9:** ecosistema pequeño.
  - **Seguir con Edge en modo kiosco:** es lo que el cliente rechaza («abre el navegador»).
- **Seguridad del contenedor:**
  - Las páginas remotas (las del backend) **no tienen acceso a IPC de Tauri**: el archivo de
    capacidades solo permite IPC a `tauri://localhost`, que son las páginas locales del visor.
  - La navegación está limitada a los servidores configurados.
  - Sin `devtools` en las versiones publicadas. En las de prueba se activan con una variable de entorno (§4.6).
  - **Certificado del servidor remoto:** Tauri no expone el evento `ServerCertificateErrorDetected`
    de WebView2 ([especificación](https://github.com/MicrosoftEdge/WebView2Feedback/blob/main/specs/ServerCertificate.md)),
    y hay un fallo conocido con redirecciones
    ([WebView2Feedback #4575](https://github.com/MicrosoftEdge/WebView2Feedback/issues/4575)). La
    forma de fijar el certificado se decide en la prueba **S5** (§6.1), ver §2.3.

### 1.2 Runtime de Python: **Python 3.12 embebible oficial + `site-packages` preparado en CI** (sin empaquetador)

- **Qué:** el zip `python-3.12.x-embed-amd64.zip` (licencia PSF, binarios firmados por la PSF:
  **verificar** la firma Authenticode en el CI) con un `site-packages` **ya instalado en el CI**:
  ```
  python -m pip install --target build/runtime/Lib/site-packages \
      --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 \
      --only-binary=:all: --no-deps --require-hashes \
      -r requirements-vms.txt -r requirements-analytics.txt -r requirements-central.txt \
      -r distribution/runtime/requirements-windows-extra.txt
  ```
  `python312._pth` incluye `Lib\site-packages` y `..\app` (el código del producto, §2.4).
- **Bytecode:** los `.pyc` se compilan **en CI** (`python -m compileall --invalidation-mode
  unchecked-hash`), y los servicios corren con `PYTHONDONTWRITEBYTECODE=1`. Así el intérprete nunca
  escribe dentro de `versions\` (carpetas de solo lectura y con enlaces duros compartidos entre
  versiones, §2.4) y el payload sale igual en dos builds.
- **Por qué:**
  - **En el PC del cliente nunca se ejecuta pip.** Desaparecen los fallos de ayer (pip embebible, stderr de PS 5.1, comillas).
  - Instalar es copiar archivos verificados. Funciona sin Internet.
  - Los locks con hashes ya descargan para win_amd64 con código 0 (ESTADO.md).
- **Componente `runtime` del actualizador:** intérprete + `site-packages`. Cambia poco: con una
  versión nueva de Python o de alguna dependencia.
- **Descartadas:**
  - **PyInstaller 6.22.3:** GPL con excepción de bootloader. Da falsos positivos de antivirus, no
    tiene hook de OpenVINO y obligaría a reconstruir todo para un parche de código.
  - **Nuitka 4.2.2:** **AGPL-3.0 desde el 28-01-2026**, con excepción para el runtime. Choca con la
    regla de «nada AGPL».
  - **Briefcase 0.4.5:** no controla servicios.
  - **python-build-standalone** (MPL-2.0, 20261003) queda como alternativa si el embebible da guerra
    con alguna wheel. Es válido por licencia, pero hoy no hace falta.

### 1.3 Servicios de Windows: **`vmshost.exe` (arrancador fijo) + `vmsctl.exe` (por versión)**, en Rust (sustituyen a WinSW)

Dos ejecutables firmados hechos con
[windows-service-rs](https://github.com/mullvad/windows-service-rs) (Apache-2.0/MIT, v0.8.1 del
08-05-2026, activo):

- **`vmshost.exe` — el arrancador.** Vive en `C:\Program Files\VMSMultimarca\bin\` y es lo que
  registra el SCM para **todos** los servicios (`vmshost.exe service --name VMSBackend`). **No se
  actualiza nunca por la vía normal**: solo lo cambia el instalador completo. Por eso es pequeño
  (objetivo: < 600 líneas, sin dependencias de red ni de Python) y hace solo esto:
  1. Lee el puntero `ProgramData\VMSMultimarca\state\active.json` (versión activa, versión anterior,
     ranura del actualizador y si la versión está «a prueba»). Si el archivo falta o está corrupto,
     lo **reconstruye** con la última versión marcada como buena en el diario (§2.5).
  2. Lanza `versions\<X>\bin\vmsctl.exe run --service <nombre>` (o, para `VMSUpdater`,
     `updater\slot-<A|B>\vmsctl.exe run ...`) dentro de un *Job Object* «kill on close».
  3. **Vigila la versión a prueba:** si el hijo cae 3 veces en 10 min, o si pasan 30 min sin que el
     actualizador confirme la versión, devuelve el puntero a la versión (o ranura) anterior y
     arranca esa. Es lo que cubre el caso «el actualizador nuevo ni siquiera arranca».
  4. Informa al SCM. Además, el SCM tiene acciones de recuperación (§2.2).
- **`vmsctl.exe` — por versión** (`versions\<X>\bin\`), se actualiza con cada versión. Tiene dos
  papeles:
  1. **Anfitrión del proceso:** `vmsctl.exe run --service VMSBackend` lanza el proceso real.
     Hace lo siguiente:
     - Lanza `runtime\python.exe -m vms`, `engine\mediamtx.exe`, etc.
     - Lo mete en un *Job Object* (si el anfitrión muere, mueren los hijos: nunca hay procesos huérfanos).
     - Parada ordenada en 3 escalones: `POST /api/internal/shutdown` o `CTRL_BREAK`, después 10 s, y al final `TerminateJobObject`.
     - Reinicio con backoff (1, 2, 5, 10, 30 s).
     - Captura stdout/stderr, **oculta credenciales** con las mismas reglas que `vms.core.rtsp.redact` (ver la nota de abajo) y escribe `logs\<servicio>.log` con rotación de 10 × 10 MB.
     - Informa de su estado a `vmshost`.
  2. **Configuración del equipo**, usada por el instalador, el actualizador y el desinstalador.
     Sustituye a todo lo que hoy hace `install.ps1`:
     ```
     vmsctl services install  --role control|store|central|viewer --data-dir <ruta>
     vmsctl services uninstall
     vmsctl services start|stop|restart [--only VMSBackend,...]
     vmsctl firewall apply --profiles private[,domain]     (reglas por puerto, §2.6)
     vmsctl acl apply --data-dir <ruta>                    (icacls con SID, sin nombres localizados)
     vmsctl ports check                                     (puertos libres y fuera de excludedportrange)
     vmsctl health wait --timeout 120 [--deep]             (sale con 0 si ok)
     vmsctl version switch <X.Y.Z>                          (reescribe active.json de forma atómica, §2.5)
     vmsctl update check|status|rollback                    (habla con VMSUpdater por su tubería; exige elevación)
     vmsctl tls setup --hostname <nombre> [--import-root]
     vmsctl diag bundle --out <zip>                         (registros + estado, sin secretos)
     ```
     **Códigos de salida estables:**

     | Código | Significado |
     |---|---|
     | 0 | ok |
     | 2 | uso incorrecto |
     | 10 | puerto ocupado |
     | 11 | sin permisos |
     | 12 | health check fallido |
     | 20 | error de Windows (con el código Win32 en stderr) |

     La salida va en JSON con `--json`. Nunca se analiza el texto localizado de Windows.
- **Por qué:**
  - **WinSW 2.12.0** lleva parado desde enero de 2023 y su v3 sigue en alfa.
  - El proceso del servicio aparece como un ejecutable **nuestro y firmado** (antivirus, Administrador de tareas).
  - Se elimina PowerShell de la instalación, con todas las lecciones de ayer: stderr convertido en error, comillas y codificación ANSI.
  - El mismo binario sirve al instalador, al actualizador y al desinstalador. Se prueba una vez con `cargo test` y en el CI de Windows.
  - Separar `vmshost` (fijo y mínimo) de `vmsctl` (actualizable) permite parchear la lógica del
    anfitrión (redacción, registros, reinicios) por actualización sin perder nunca al vigilante.
- **Ocultación de credenciales:** las reglas se comparten mediante
  `tests/fixtures/redaction_vectors.json` (pares entrada → salida esperada). Lo comprueban a la vez
  `pytest` y `cargo test`, para que Python y Rust nunca diverjan.
- **Descartadas:**
  - **[shawl](https://github.com/mtkennerly/shawl) v1.9.0** (MIT, 03-05-2026). Es el **plan B** si `vmsctl` se retrasa: es un envoltorio genérico sin Job Object configurable, sin ocultación de credenciales y sin la parte de configuración del equipo. Si se usa, `vmshost` sigue siendo obligatorio (lanzaría shawl en lugar de `vmsctl run`).
  - **WinSW 3 alfa.**
  - **NSSM:** sin versiones desde 2017 (**no verificado** hoy).

### 1.4 Instalador gráfico: **Inno Setup 7.1.0**

- **Qué:** [jrsoftware/issrc](https://github.com/jrsoftware/issrc), tag `is-7_1_0` (12-08-2026),
  activo (último push el 04-10-2026). Licencia propia permisiva: es una herramienta de build y no
  contamina la salida. La [página de licencias](https://jrsoftware.org/isorder.php) **pide** la
  licencia comercial a quien facture más de 5.000 USD, pero dice que **no es estrictamente
  obligatoria** (perpetua, con 2 años de actualizaciones; el precio no aparece en la página: **no
  verificado**). **Decisión del usuario D3** (§7.4).
- **Asistente** (`distribution/installer/VMSMultimarca.iss`, español por defecto, inglés opcional):
  1. Bienvenida y licencia de uso (EULA de Unmanned Studio + avisos de terceros).
  2. **Tipo de puesto** (tipos de Inno con componentes):

     | Tipo | Servicios | Visor |
     |---|---|---|
     | **Puesto de control** (VMS + muros) | `VMSEngine`, `VMSBackend`, `VMSUpdater` | sí |
     | **Tienda con analítica** | lo anterior + `VMSAnalytics`, `VMSHeartbeat` | opcional |
     | **Panel central** | `VMSCentral`, `VMSUpdater` | — |
     | **Solo visor** (central de vigilancia que mira otros PC) | `VMSUpdater` | sí |
  3. **Grabaciones:** carpeta y disco. Página propia en Pascal Script con:
     - espacio libre;
     - aviso si es el disco del sistema;
     - estimación de días de grabación con N cámaras a X Mbit/s;
     - bloqueo si quedan menos de 50 GB.
  4. **Sede:** nombre, código de tienda, `site_id` (sugerido) y, opcionalmente, URL del panel central y token de la sede.
  5. **Seguridad:**
     - Crear el administrador (usuario y contraseña ≥ 8; se pasa al backend por un archivo temporal con ACL de SYSTEM, que se borra en el primer arranque, nunca por línea de órdenes).
     - HTTPS para la red local (casilla marcada).
     - Perfil de red: «Privada» (y «Dominio»). Si la red activa es Pública, avisa y ofrece cambiarla.
  6. **Resumen** e instalación. Al terminar, `vmsctl health wait --timeout 120`: si falla, muestra
     el diagnóstico y el botón «Guardar informe» (`vmsctl diag bundle`).
- **Silenciosa (147 tiendas):**
  ```
  VMSMultimarca-Setup-2.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="C:\vms-install.log" ^
      /TYPE=store /LOADINF="tienda.inf"
  ```
  `tienda.inf` lleva el `site_id`, la central y la carpeta de grabaciones. El token de la sede y la
  contraseña inicial **no van en el .inf**: van en un archivo `secrets.json` aparte, indicado con
  `/SECRETS=`, que el instalador lee y **borra**.
- **Desinstalador:** pregunta «¿Conservar grabaciones y configuración?» (sí por defecto). En
  silencio se conservan salvo `/PURGE`. Ejecuta `vmsctl services uninstall` y `vmsctl firewall remove`.
- **Firma:** directiva `SignTool=` (instalador y desinstalador) usando `jsign` (§1.6).
- **Instalador y actualizador comparten el registro de versión:**
  - El actualizador, tras cada actualización o rollback que termina bien, escribe `DisplayVersion`
    en la clave de desinstalación de Inno (`HKLM\...\Uninstall\{AppId}_is1`) y la versión exacta en
    `HKLM\SOFTWARE\VMSMultimarca\InstalledVersion`.
  - El instalador lee esa clave en `InitializeSetup` y **se niega a bajar de versión** («Ya está
    instalada la 2.1.3, más nueva que este instalador»). Solo con `/ALLOWDOWNGRADE` (soporte
    técnico, queda en el registro).
  - Antes de tocar nada, el instalador pide el cerrojo del actualizador (por su tubería, §2.2) para
    que no haya dos instalaciones a la vez.
- **Sistemas admitidos:** Windows 11 (23H2 o posterior) y Windows Server 2022/2025 (**no
  verificado** en el laboratorio). **Windows 10 22H2 está fuera de soporte desde el 14-10-2025**: solo
  se admite si el equipo está en el programa de pago ESU (decisión **D11**). El instalador avisa en
  Windows 10 y no bloquea.
- **WebView2:** en Windows 11 ya viene. En Windows 10 22H2 el instalador comprueba la clave de
  registro de WebView2 y, si falta, ejecuta el *Evergreen Standalone Installer* incluido. Hay que
  revisar las condiciones de redistribución de Microsoft (**no verificado**).
- **Descartadas:**
  - **WiX 7:** MS-RL más el acuerdo OSMF de pago a partir de 10.000 USD de facturación; MSI es más difícil. **Solo** se haría si un cliente exige MSI para GPO/Intune (decisión D8).
  - **NSIS** (zlib): válido, pero con un asistente menos cómodo de mantener. Es la alternativa si Inno se complica.
  - **Velopack 1.2.161:** instala por usuario, sin servicios.
  - **MSIX:** no encaja con servicios y cuentas virtuales (**no verificado** a fondo).

### 1.5 Actualizador: **servicio `VMSUpdater` con python-tuf 7.0.1 (ngclient)**

- **Qué:** [theupdateframework/python-tuf](https://github.com/theupdateframework/python-tuf),
  Apache-2.0 (archivo `LICENSE` leído), v7.0.1 del 01-09-2026, activo. Un paquete nuestro,
  `updater/vms_updater`, que corre como servicio con **su propia copia del runtime** (ranuras A/B
  elegidas por `vmshost`, §1.3 y §2.5).
  Diseño completo en §2.5 y formato del manifiesto en §2.7.
- **Por qué:**
  - Ningún framework actualiza **servicios** con migración, health check y rollback.
  - TUF resuelve lo difícil de la seguridad: claves con umbral, rotación, protección contra versiones anteriores y contra metadatos congelados.
  - Python ya es el lenguaje del producto.
  - Se toma como referencia de diseño [Fleet orbit](https://github.com/fleetdm/fleet/tree/main/orbit) (MIT fuera de `ee/`), sin copiar código.
- **Descartadas:**
  - **Tauri updater:** solo actualiza el visor.
  - **WinSparkle 0.9.4:** sin rollback ni umbral.
  - **Velopack:** por usuario; además, su issue #1040 dice que los hooks corren sin elevar en modo MSI.
  - **Omaha:** archivado.
  - **PyUpdater:** archivado.
  - **tufup 0.10.0:** fija `tuf==4.0.*`, afectada por GHSA-qp9x-wp8f-qgjj.
  - **[awslabs/tough](https://github.com/awslabs/tough)** (cliente TUF en Rust, MIT/Apache-2.0
    según sus archivos `LICENSE-MIT`/`LICENSE-APACHE`; activo, tuftool v0.17.0 del 10-07-2026). Es
    la **alternativa** si en el futuro el actualizador se pasa a Rust dentro de `vmsctl`. Hoy no
    compensa reescribir en Rust la lógica crítica que python-tuf, la implementación de referencia,
    ya resuelve.
- **El visor no tiene un actualizador propio.** Lo actualiza `VMSUpdater` como un componente más
  (§2.5). Así hay un solo sistema de actualizaciones.

### 1.6 Firma de código (Authenticode) y claves de actualización (TUF)

Son **dos firmas distintas**:

| Firma | Para qué | Qué se firma | Herramienta |
|---|---|---|---|
| **Authenticode** | SmartScreen, antivirus y EDR. El actualizador también la comprueba como segunda capa | `vmshost.exe`, `vmsctl.exe`, `VMS.exe`, `mediamtx.exe` (binario MIT de terceros: lo redistribuimos nosotros y lo firmamos como distribuidores), instalador, desinstalador y **todo PE sin firma** de nuestro payload (`.pyd`/`.dll` de wheels que no traigan firma). `python.exe`/`python312.dll` ya vienen firmados por la PSF: no se tocan | [jsign 7.5](https://github.com/ebourg/jsign) (Apache-2.0), desde Linux/macOS/Windows |
| **TUF** | Que el actualizador solo instale lo que publicamos nosotros | Metadatos `root`, `targets`, `snapshot`, `timestamp` | `tools/release/tuf_repo.py` (python-tuf `repository` + `securesystemslib`) |

**Reglas de Authenticode que no rompen el rollback:**
- **Siempre con sello de tiempo RFC 3161** (`jsign --tsaurl`). Así una firma sigue siendo válida
  cuando el certificado caduca o se renueva (máximo 459-460 días por certificado desde 2026).
- El actualizador **no fija la huella** del certificado. Comprueba con `WinVerifyTrust`: cadena hasta
  una raíz de confianza de Windows, uso «firma de código», sello de tiempo válido y **sujeto** con
  `O=<razón social de Unmanned Studio>` y `C=ES`, más el emisor esperado (lista corta de CA admitidas
  en `release.json`, que viene firmado por TUF). Así una versión antigua firmada con el certificado
  anterior sigue pasando, y el rollback funciona tras renovar.
- La protección principal es TUF (hash y longitud firmados). Authenticode es la segunda capa.

**Proveedor de Authenticode (decisión D1):**
- **Certum OV** (desde 209 €; máximo 459 días por certificado). **Es la opción por defecto.** Su firma
  en la nube (SimplySign) suele pedir una app de escritorio con sesión iniciada: que firme sin
  intervención desde CI está **no verificado**. No importa, porque la publicación se firma en local
  de todas formas (ver «Dónde se firma» más abajo).
- **Azure Artifact Signing** (~9,99 USD/mes): solo para **organizaciones con al menos 3 años de
  historial verificable**; los moderadores de Microsoft lo confirman en 2026
  ([Q&A](https://learn.microsoft.com/en-us/answers/questions/5972282/does-azure-artifact-signing-still-require-3-years)).
  En la UE no está disponible para particulares. Solo es opción si Unmanned Studio es una sociedad
  con 3 años o más.
- **EV:** no compensa. Desde 2024 ya no salta el aviso de SmartScreen de entrada.

**Claves TUF (decisión D4):**

| Rol | Tipo | Umbral | Dónde vive | Caducidad |
|---|---|---|---|---|
| `root` | **ECDSA P-256** | **2 de 3** | Llave 1: YubiKey A · Llave 2: YubiKey B · Llave 3: clave en papel (generada sin conexión, impresa como QR cifrado con frase de paso) en una **caja fuerte**. Nunca en CI ni en un PC conectado | 1 año |
| `targets` | **ECDSA P-256** | 1 de 1 | YubiKey A (ranura PIV distinta de la de `root`). **Se firma en local**, nunca en CI | 90 días |
| `snapshot` + `timestamp` | ed25519 | 1 de 1 | Secreto del flujo programado `timestamp.yml` de GitHub | 30 días / **7 días** (re-firma diaria) |
| `offline-timestamp` (repositorio para espejos, §1.7) | ed25519 | 1 de 1 | En local, junto a `targets` | **60 días** |

- `root` y `targets` son ECDSA P-256 porque el `HSMSigner` de securesystemslib
  [solo admite ECDSA P-256 y P-384](https://github.com/secure-systems-lab/securesystemslib/blob/main/securesystemslib/signer/_hsm_signer.py)
  (leído el 5-oct-2026), no ed25519. TUF permite mezclar tipos de clave entre roles. Que la firma
  PKCS#11 con YubiKey funcione de punta a punta con python-tuf 7 se comprueba en **S3**.
- **Sé honesto con lo que da el umbral:** si las dos YubiKey las guarda la misma persona, el umbral 2
  de 3 protege contra **perder o que roben una llave**, no contra esa persona. Para lo segundo hace
  falta una segunda persona con la YubiKey B (decisión D4).

**Dónde se firma (por qué la cuenta de GitHub sola no basta para atacar):**
1. CI (`build.yml`) compila, genera el SBOM y la atestación de procedencia, y deja los artefactos
   **sin publicar**.
2. En el PC de publicación, `python -m tools.release publish --version X.Y.Z`:
   - descarga los artefactos y comprueba la atestación (`gh attestation verify`) y los hashes;
   - firma con Authenticode (Certum) los PE;
   - firma `targets` con la YubiKey A (pide PIN y toque físico);
   - sube a R2 y avisa a CI para que firme `snapshot`/`timestamp`.
3. Si roban la cuenta de GitHub, el atacante tiene `snapshot` y `timestamp`, pero **no puede
   autorizar ni un solo paquete nuevo**: eso lo hace `targets`, que está en la llave física. Lo peor
   que puede hacer es dejar de refrescar el `timestamp` (las tiendas dejan de actualizar y lo avisan).

**Procedimiento de compromiso** (`docs/PUBLICAR-VERSION.md`, sección «Si roban una llave»), con
**ensayo en CI** (`tests/updater/test_key_compromise.py`) y un ensayo real al año:
1. Con 2 de las 3 llaves `root`, publicar un `root` nuevo que sustituye la clave comprometida.
2. Re-firmar `targets` (y `snapshot`/`timestamp` si toca) con las claves nuevas.
3. Publicar. Los clientes siguen la cadena de `root` y dejan de aceptar la clave vieja.
4. Si la comprometida es Authenticode: revocar el certificado con Certum y publicar una versión
   firmada con el nuevo (el actualizador comprueba sujeto y CA, no huella, así que lo acepta).

### 1.7 Alojamiento de actualizaciones: **Cloudflare R2 + Worker**

- **Bucket R2** `vms-updates`:
  - `online/metadata/` (JSON de TUF, público, caché corta).
  - `online/targets/` (paquetes; solo mediante el Worker).
  - `offline/` (repositorio para espejos USB, ver abajo).
- **Worker** `infra/update-worker/` (TypeScript, sin dependencias de terceros):
  - `GET /metadata/*`: libre. Los metadatos TUF no son secretos y van firmados.
  - `GET /targets/*`: exige `Authorization: Bearer <token de sede>`, que se valida contra un KV
    `site_tokens` (clave `<cliente>:<hash SHA-256 del token>`, con sede y activa sí/no).
  - Registra la descarga (cliente, sede, versión).
- **Modelo multicliente:**
  - **El panel central de un cliente no tiene nunca credenciales de Cloudflare.** Los tokens de sede
    los da de alta y los revoca Unmanned con `python -m tools.release site-token add|revoke
    --client covert --site S0042` (escribe en el KV con un token de API de Cloudflare que solo vive
    en el PC de publicación).
  - El token se entrega al instalador de la tienda en `secrets.json` (§1.4).
  - Cada cliente tiene su prefijo en el KV: un token de un cliente no puede descargar como otro
    cliente, y revocar un cliente entero es borrar su prefijo.
- **Coste:** 0,015 $/GB-mes con 10 GB gratis y salida gratuita. Un paquete de código pesa unos MB, el
  runtime unos 60-90 MB y los modelos unos 100 MB (**estimaciones**). Con 150 sedes se queda en
  **0-5 €/mes**. El plan gratuito de Workers (100 000 peticiones/día) sobra: 150 sedes × 4
  comprobaciones al día = 600.
- **Sin Internet en la tienda (espejo USB o carpeta compartida):**
  - Hay un **segundo repositorio TUF, `offline`**, con su propio `root` (mismas llaves físicas), los
    mismos paquetes y un `timestamp`/`snapshot` que caducan a los **60 días** (clave
    `offline-timestamp`, firmada en local al preparar el USB).
  - La sede se instala en modo `online` o `offline` (con su `root` de confianza correspondiente);
    cambiar de modo es una acción del instalador, no del actualizador.
  - La verificación TUF es idéntica: la seguridad no depende del transporte.
  - El USB se prepara con `python -m tools.release mirror --version 2.1.0 --out E:\`, que firma un
    `offline-timestamp` nuevo. Un USB sirve durante 60 días desde que se prepara.
  - Se fija con `VMS_UPDATE_MIRROR=file:///D:/vms-updates`.
  - **Nunca se ignora la caducidad.** Si los metadatos han caducado o el reloj del equipo está
    desfasado, **no se aplica** la actualización y se avisa (§2.5 paso 1).
- **Descartado:** GitHub Releases en repo privado, porque obligaría a llevar un token embebido; y en
  repo público, por el límite de peticiones y la exposición.

### 1.8 Compilar y probar en Windows

**Con GitHub (recomendado):** repositorio **privado** con GitHub Actions (§4.6):

| Flujo | Cuándo | Qué hace |
|---|---|---|
| `ci.yml` | cada push y PR | `ruff` + `mypy` (paquetes con tipos) + `pytest -m "not e2e and not slow"` en `ubuntu-latest`; `pytest` completo en **`windows-latest`** (MediaMTX Windows, Chromium de Playwright, ffmpeg de pruebas); `cargo test` y `cargo clippy -D warnings` en `native/`; comprobación de voseo (§4.1) |
| `build.yml` | PR a `main`, tags `v*` y manual | runtime (con `.pyc` compilados) → `vmshost.exe`, `vmsctl.exe` y `VMS.exe` (`cargo build --release`, `cargo tauri build --no-bundle`) → payload por versión → ISCC → artefactos **sin firma de publicación** + SBOM (CycloneDX) + `attest-build-provenance` |
| `e2e-windows.yml` | tras `build.yml` en `main` y en tags | instalación silenciosa, servicios, visor por CDP, actualización v1→v2 con medición del corte, rollback de una v3 rota, fallos inyectados en cada paso del diario y desinstalación (§4.6) |
| `publish-meta.yml` | lo dispara `tools.release publish` | firma `snapshot` y `timestamp` y los sube. **No** tiene acceso a `targets` ni a Authenticode |
| `timestamp.yml` | cron diario | re-firma `timestamp` (caduca en 7 días) |

**Sin GitHub (o mientras no esté):** los flujos **solo llaman a scripts del repositorio**, así que
lo mismo funciona a mano en cualquier Windows 10/11 x64:
```
py -3.12 -m tools.build all --version 2.0.0-dev.5 --out dist\     # runtime, nativos, payload, instalador
py -3.12 -m tools.build sign --dist dist\                           # si hay certificado
py -3.12 -m tests.windows.run_e2e --installer dist\VMSMultimarca-Setup-2.0.0-dev.5.exe
```
- La máquina puede ser el PC Windows 11 de las pruebas, con una **instantánea de VM limpia**
  (Hyper-V en Windows Pro o VMware Workstation) para volver al estado inicial entre pruebas. La VM
  Hyper-V también sirve para las pruebas de **corte de luz** (§4.4). Más adelante, ese mismo PC puede
  ser un *runner* propio de GitHub.
- Desde el Mac **no** se compila el instalador. La imagen con Wine `amake/innosetup-docker` se queda
  en Inno 6.7.1. Windows 11 ARM en UTM no es representativo para servicios ni WebView2 (x64
  emulado).
- En el Mac sí se pueden hacer el runtime (`pip install --platform win_amd64`), los zips, la firma
  con jsign y todo `pytest`.

---

## 2. Arquitectura de procesos en Windows

### 2.1 Mapa

```
┌──────────────────────────── PC Windows (puesto de control / tienda) ────────────────────────────┐
│ Sesión 0 (servicios, arrancan sin nadie conectado). El SCM siempre lanza bin\vmshost.exe,       │
│ que lee state\active.json y lanza la versión activa (§1.3).                                      │
│                                                                                                  │
│  VMSEngine     NT SERVICE\VMSEngine    vmshost → vmsctl run → mediamtx.exe   RTSP→graba, WebRTC  │
│      ▲  rutas en mediamtx\mediamtx.yml (fuente única) · API 127.0.0.1:9997 solo lectura/estado   │
│      │                                                    ▲ log redactado (logs\engine.log)      │
│  VMSBackend    NT SERVICE\VMSBackend   vmshost → vmsctl run → python -m vms --engine attach      │
│      │  escribe mediamtx.yml de forma atómica · :8600/:8643                                      │
│  VMSAnalytics  NT SERVICE\VMSAnalytics vmshost → vmsctl run → python -m analytics                │
│  VMSHeartbeat  NT SERVICE\VMSHeartbeat vmshost → vmsctl run → python -m central.agent            │
│  VMSUpdater    LocalSystem             vmshost → updater\slot-A|B\vmsctl run → python -m vms_updater │
│      control local por tubería \\.\pipe\VMSMultimarca.updater (SYSTEM + Administradores elevados) │
│                                                                                                  │
│ Sesión del usuario (operador o cuenta de muros)                                                  │
│  VMS.exe (Tauri, una instancia)  ── bandeja ── ventana «Panel» ── «Muro 1..4» (una por monitor)  │
│        └── WebView2 (un proceso de GPU compartido) ── HTTP(S) al backend · WebRTC directo a 8189 │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Servicios y cuentas virtuales

| Servicio | Cuenta | Inicio | Dependencias | Permisos sobre datos (`%ProgramData%\VMSMultimarca`) |
|---|---|---|---|---|
| `VMSEngine` | `NT SERVICE\VMSEngine` | automático | — | `mediamtx\` lectura (incluye el YAML con las rutas); `recordings\` modificar; `logs\engine*` escribir |
| `VMSBackend` | `NT SERVICE\VMSBackend` | automático (retrasado) | ninguna en el SCM: el backend arranca aunque el motor no esté y se conecta cuando aparece | `config\`, `secrets\`, `logs\` modificar; `recordings\` modificar (disk guard); `mediamtx\` modificar |
| `VMSAnalytics` | `NT SERVICE\VMSAnalytics` | automático (retrasado) | — | `analytics\` modificar; `secrets\internal.token` leer |
| `VMSHeartbeat` | `NT SERVICE\VMSHeartbeat` | automático (retrasado) | — | `secrets\site.token` leer |
| `VMSCentral` | `NT SERVICE\VMSCentral` | automático | — | `central\` modificar |
| `VMSUpdater` | **LocalSystem** | automático (retrasado) | — | todo (para respaldar, restaurar y parar o arrancar servicios) |

- Las ACL se aplican con `vmsctl acl apply` usando **SID** (`*S-1-5-80-<hash del servicio>`,
  `*S-1-5-18`, `*S-1-5-32-544`), nunca nombres localizados. Por defecto, nadie más tiene acceso:
  los usuarios del equipo no ven grabaciones ni secretos. Esto cierra el pendiente 1 de ESTADO.md.
- **Recuperación del SCM** en todos los servicios: reiniciar a los 1, 5 y 30 s y poner el contador a
  cero a las 24 h. Por dentro, `vmsctl` reinicia el proceso hijo con backoff, y `vmshost` vigila la
  versión a prueba (§1.3).
- **Secretos** (`secret.key`, `internal.token`, contraseñas de usuario del motor, `site.token`):
  - Se cifran con **DPAPI de máquina** (`CryptProtectData` con `CRYPTPROTECT_LOCAL_MACHINE`) y el
    archivo lleva una **ACL que solo deja entrar al SID del servicio que lo usa**, a SYSTEM y a
    Administradores. **La barrera real es la ACL**: DPAPI de máquina lo puede descifrar cualquier
    proceso del equipo; lo que añade es que una copia del archivo fuera del equipo no sirve.
  - **No se usa DPAPI-NG con `SID=`**: según la
    [documentación de Microsoft](https://learn.microsoft.com/en-us/windows/win32/seccng/protection-descriptors),
    ese protector es para grupos de un bosque de Active Directory, y los PC de tienda no están en
    dominio. `LOCAL=machine` de DPAPI-NG equivale en la práctica a DPAPI de máquina; se elige el
    clásico por ser más simple y estar en todas las versiones.
  - Migración automática desde el `secret.key` en claro de la v1: se lee, se cifra y se sobrescribe
    el original antes de borrarlo (en SSD, el borrado seguro **no** está garantizado: se documenta).
  - Código en `vms/core/winsec.py` con `ctypes` (sin dependencias nuevas).
- **¿Por qué `VMSUpdater` es LocalSystem?** Necesita parar y arrancar servicios, escribir en
  `Program Files` y cambiar el puntero de versión. Para reducir lo que puede atacarse:
  - **no escucha en TCP**: su control local es la tubería `\\.\pipe\VMSMultimarca.updater` con ACL
    solo para SYSTEM y Administradores. Un administrador desde una sesión normal tiene el token
    filtrado por UAC, así que para usarla hay que **elevar** (`vmsctl update rollback` pide UAC);
  - no ejecuta nada que no venga en un `target` TUF verificado y con Authenticode válido (§1.6);
  - no interpreta datos de las cámaras.

### 2.3 App de escritorio (`VMS.exe`)

**Instalación.** Por máquina, en `C:\Program Files\VMSMultimarca\versions\<X>\viewer\VMS.exe`. El
acceso directo del menú Inicio («VMS Multimarca») apunta a `bin\vmshost.exe viewer`, que abre el
visor de la versión activa (no hay enlaces de carpeta que se puedan quedar a medias).
- Arranque con la sesión en `HKCU\...\Run`, según usuario.
- En un puesto de control, el asistente ofrece «Abrir los muros al iniciar sesión» para la cuenta de
  muros (`HKLM\...\Run` con `bin\vmshost.exe viewer --walls`).
- Instancia única con `tauri-plugin-single-instance` 2.5.2: un segundo clic trae la ventana al frente.

**Bandeja (icono con el color del estado de `/api/health`: verde, ámbar o rojo).** Menú:
- Abrir panel.
- Muros: Mostrar u ocultar todos, Muro 1-4 y Reasignar monitores….
- Estado del sistema.
- Actualizaciones: «Al día 2.1.0», «Instalada 2.1.1: reiniciar el visor» o «Buscar ahora». «Volver a
  la versión anterior…» lanza `vmsctl update rollback` **con elevación (UAC)**: sin credenciales de
  administrador, no se puede.
- Servidores… (puesto remoto).
- Acerca de (versión, avisos de terceros).
- Salir. No para los servicios: la grabación sigue.

**Ventanas:**
- **Panel:** ventana normal con la interfaz completa.
- **Muro N:** sin bordes, a pantalla completa en el monitor asignado.
  - La asignación monitor → muro se guarda en `%APPDATA%\VMSMultimarca\viewer.json` con la clave
    `nombre del monitor + posición + resolución`.
  - Con `availableMonitors()` cada 5 s se detecta si se conecta o desconecta un monitor. Si
    desaparece uno, su muro se oculta. Si vuelve, reaparece.
  - DPI por monitor: se usa `scaleFactor` de cada monitor.
- **Todas las ventanas comparten la misma carpeta de datos de WebView2** (un único proceso de GPU,
  según MS Learn). Que esto mejore la decodificación por hardware con 4×16 flujos está **no
  verificado**: lo mide S1.

**Comunicación con el servicio:**
1. **HTTP(S) al backend**, igual que hoy el navegador. Local: `http://127.0.0.1:8600`. Remoto:
   `https://<host>:8643`, con **fijación del certificado** (el visor guarda la huella SHA-256 la
   primera vez que conecta, el usuario la confirma, y avisa si cambia, como SSH). Cómo se hace se
   decide en **S5** (§6.1), entre dos opciones:
   - **A:** manejador COM propio de `ServerCertificateErrorDetected` (WebView2) desde
     `with_webview` de Tauri, que acepta solo la huella guardada. Hay que esquivar el fallo con
     redirecciones ([#4575](https://github.com/MicrosoftEdge/WebView2Feedback/issues/4575)).
   - **B:** el visor importa el certificado del servidor (autofirmado, con restricción de nombre) en
     el almacén de confianza **del usuario** tras confirmarlo, y además comprueba la huella él
     mismo con una conexión TLS previa en Rust antes de abrir la ventana.
   - Criterio de S5: la opción que pase las pruebas de certificado cambiado, redirección y
     reconexión. Si ninguna pasa, la v2 sale con el puesto remoto solo en red local con la opción B.
2. **Muros sin escribir contraseñas en el PC de control:** se mantiene el mecanismo de la v1 (token
   de kiosco que ya acepta el backend), pero **sin pasarlo por la línea de órdenes**:
   - El token vive en `ProgramData\VMSMultimarca\secrets\kiosk.token` con ACL de lectura para
     SYSTEM, Administradores y el grupo local **`VMS Operadores`** (lo crea el instalador y mete en
     él al usuario que instala y a la cuenta de muros).
   - El visor lo lee al abrir un muro y lo cambia por la cookie de kiosco firmada (como hoy).
   - Diferencia honesta con los códigos de un solo uso: un token copiado sirve hasta que se rota.
     Se rota con `vmsctl kiosk rotate` (y el instalador lo rota al reinstalar).
   - El emparejamiento por tubería con códigos de un solo uso **queda para la v2.1** (§8).
3. **Panel:** inicio de sesión normal (operador o admin) con «Recordarme en este equipo». La cookie
   persistente está en el perfil de WebView2 del usuario.
4. **Puesto remoto (central de vigilancia):** «Servidores…» guarda una lista de PC (nombre, URL,
   huella). Cada muro se asocia a un servidor y a un muro de ese servidor. El muro remoto usa el
   inicio de sesión de kiosco del servidor remoto (usuario de solo vista creado allí). **Mezclar
   cámaras de varias sedes en un mismo muro queda para la v2.1.**

**Arranque sin backend:** página local «Conectando con el servicio…» con reintentos cada 2 s, y
botón «Ver diagnóstico», que muestra el estado de los servicios leído de
`updater\public-status.json`, legible por Usuarios.

### 2.4 Disposición en disco

```
C:\Program Files\VMSMultimarca\
├── bin\
│   └── vmshost.exe        (arrancador fijo; solo lo cambia el instalador)
├── versions\
│   ├── 2.0.0\
│   │   ├── bin\vmsctl.exe
│   │   ├── runtime\            python.exe, python312.dll, python312._pth, Lib\site-packages\ (.pyc ya compilados)
│   │   ├── app\                vms\, analytics\, central\  (código Python y web)
│   │   ├── engine\             mediamtx.exe, MEDIAMTX-LICENSE.txt
│   │   ├── viewer\             VMS.exe
│   │   ├── models\             rfdetr-nano.xml/.bin, rfdetr-small.xml/.bin, LICENSE
│   │   ├── THIRD_PARTY_NOTICES.txt
│   │   └── release.json        (copia del descriptor firmado de §2.7)
│   └── 2.1.0\ ...
└── updater\
    ├── slot-a\  (vmsctl.exe + runtime mínimo + vms_updater)
    └── slot-b\

C:\ProgramData\VMSMultimarca\       (igual que hoy, §3.2 del contrato) +
├── state\     active.json (puntero), journal.json (diario), ambos escritos de forma atómica
├── backups\pre-2.1.0-20261120T031500Z\   config\, users.json, secrets\ (cifrados), pg_dump opcional
├── updater\   public-status.json (lectura para Usuarios), cache\ (TUF), blacklist.json
```

- **Sin junctions.** Cambiar una junction exige borrarla y crearla de nuevo: si se corta la luz entre
  los dos pasos, no queda versión activa. En su lugar, `active.json` se escribe así: archivo temporal
  en la misma carpeta → `FlushFileBuffers` → `MoveFileExW(MOVEFILE_REPLACE_EXISTING |
  MOVEFILE_WRITE_THROUGH)`. Lo mismo para `journal.json`, `config.json`, `users.json` y
  `mediamtx.yml` (función común `atomic_write` en Python y en Rust, con pruebas).
- Los componentes que no cambian entre versiones se **copian con enlaces duros** desde la versión
  anterior (NTFS), así que ocupan disco una sola vez. Es seguro porque nadie escribe dentro de
  `versions\` (`PYTHONDONTWRITEBYTECODE=1`, §1.2).
- Las carpetas de `versions\` son **inmutables**: solo lectura para todos excepto el actualizador.
- Que se pueda borrar una versión antigua mientras un proceso usa un ejecutable con enlace duro está
  **no verificado**: si falla, el actualizador lo reintenta en el siguiente ciclo. Va en el e2e.

### 2.5 Cómo se actualiza todo y cuánto se corta la grabación

**Objetivo medible (lo comprueba el e2e de Windows):**

| Qué cambia | Servicios reiniciados | Corte de grabación | Corte de vista en vivo |
|---|---|---|---|
| `app` (código Python o web) | `VMSBackend`, `VMSAnalytics`, `VMSHeartbeat` | **0 s** (el motor no se toca) | 0 s en los muros abiertos: el vídeo WebRTC va directo del motor al visor. La interfaz se recarga una vez |
| `runtime` (Python o dependencias) | ídem | **0 s** | ídem |
| `models` | `VMSAnalytics` | 0 s | 0 s |
| `viewer` | — (el visor se reinicia solo, ~3 s, cuando está inactivo; los muros, de inmediato) | 0 s | ~3 s |
| `engine` (MediaMTX) | `VMSEngine` | **≤ 15 s**, solo en la ventana de mantenimiento | ≤ 15 s |
| `updater` | `VMSUpdater` (ranura A/B) | 0 s | 0 s |
| rollback automático | según los componentes revertidos | lo mismo que arriba | lo mismo |

Para que la grabación no se corte ni dependa del backend, **MediaMTX deja de ser hijo del backend**
(cambio de arquitectura; contrato §4.3/§5.2):
- `VMSEngine` ejecuta `mediamtx.exe` con `mediamtx\mediamtx.yml`.
- **El YAML es la única fuente de verdad de las rutas** (cambia el contrato §4.3):
  - Lo escribe el backend con `atomic_write` cada vez que cambia una cámara, y `python -m vms
    engine-config` al instalar o actualizar.
  - MediaMTX vigila el archivo y lo recarga. En la rama `main` de MediaMTX (código leído el
    5-oct-2026, `internal/core/path_manager.go`), la recarga **solo cierra las rutas cuya
    configuración cambió** y aplica en caliente los ajustes de grabación (`pathConfCanBeUpdated`).
    Hay que confirmarlo con v1.21.1 en **S2**, junto con que el renombrado atómico dispara bien la
    recarga.
  - Así, si el motor se reinicia con el backend caído, **vuelve a grabar solo**: sus rutas están en
    su archivo. Esto sustituye al hallazgo de la v1 («la recarga borra las rutas añadidas por la
    API»): ya no hay rutas añadidas por la API.
  - **Coste:** las URL de las cámaras (con contraseña) quedan en el YAML en disco. Se mitiga con la
    ACL de `mediamtx\` (solo `VMSEngine`, `VMSBackend`, SYSTEM y Administradores) y el YAML nunca
    entra en `diag bundle` ni en respaldos sin cifrar. Es la misma protección real que DPAPI de
    máquina (§2.2). Si S2 falla, la alternativa es que `vmsctl run` del motor vuelva a cargar las
    rutas desde una copia cifrada al arrancar MediaMTX, también sin depender del backend.
- El backend arranca en **modo `attach`**: no lanza el proceso; usa la API `127.0.0.1:9997` solo para
  leer estado (`/v3/paths/list`, `/v3/info`) y para el proxy WHEP. Vigila al motor cada **2 s**.
- **La pausa por contraseña rechazada (401)** quita las rutas del equipo **del YAML**. Como ya no lee la
  salida estándar de MediaMTX:
  - `vmsctl` escribe `logs\engine.log` ya **redactado**.
  - El backend lo sigue con un lector incremental (`vms/engine/logtail.py`) que tolera la rotación del archivo.
  - Que MediaMTX nunca escriba credenciales en su registro está **no verificado**. La redacción en
    `vmsctl` cubre ese riesgo y la vigila `tests/fixtures/redaction_vectors.json`.
- Se mantiene el **modo `child`** (comportamiento actual) para desarrollo en macOS y Linux:
  `VMS_ENGINE_MODE=child|attach`.

**Diario de la actualización (`state\journal.json`).** Cada paso se anota **antes** de hacerlo y se
marca hecho **después**, siempre con `atomic_write`. Al arrancar, `VMSUpdater` lee el diario y
**retoma o revierte** (cada paso es idempotente). Si el actualizador no arranca, `vmshost` revierte
el puntero solo (§1.3). Estados: `idle → downloaded → backed_up → stopping → switched (punto de
compromiso: active.json apunta a la nueva, «a prueba») → migrated → started → verifying → good` o
`→ rolling_back → rolled_back`.

**Flujo del actualizador** (`updater/vms_updater/`):

1. **Comprobar.** Cada 6 h con ±30 min aleatorios, al arrancar, o cuando el panel central lo pide
   (`update_check` en la respuesta del latido).
   - `ngclient.Updater.refresh()` (orden `root → timestamp → snapshot → targets`, lo garantiza python-tuf).
   - **Reloj:** si el reloj del equipo está desfasado más de 5 min respecto a la cabecera `Date` del
     Worker (HTTPS), **no se actualiza**: se informa `clock_skew` en el latido y en la bandeja, y se
     sigue con la versión actual. Nunca se ignora la caducidad de TUF.
   - Si los metadatos caducaron (Worker caído, USB viejo), igual: `metadata_expired`, sin aplicar.
2. **Elegir versión:**
   - Canal de la sede: `stable` por defecto, `pilot` para tiendas piloto. El panel central puede
     cambiar el canal de una sede (solo entre canales firmados), **retener** las actualizaciones de
     una sede o pedir su rollback. No puede elegir una versión que no esté en un canal firmado.
   - La versión elegida **tiene que estar** en `targets` firmado, cumplir `min_from` y ser **mayor**
     que la instalada. Bajar de versión solo se hace por rollback, nunca porque lo diga el canal.
   - (El porcentaje por canal y la asignación de versión firmada por el panel quedan para la v2.1,
     §8. Con 147 tiendas basta `pilot` → `stable` por tandas, cambiando el canal de las sedes.)
3. **Descargar y verificar.**
   - Descarga solo los componentes cuyo `sha256` difiere de los de la versión actual.
   - Los guarda en `cache\` y los verifica: longitud y SHA-256 por TUF, y **Authenticode** de todo PE
     (sujeto, CA y sello de tiempo, §1.6; o PSF para `python.exe`/`python312.dll`).
   - Descomprime en `versions\X.Y.Z.tmp\`, enlaza con enlaces duros lo que no cambia, comprueba
     `MANIFEST.sha256` y renombra a `versions\X.Y.Z\`.
4. **Esperar a la ventana de mantenimiento.**
   - Por defecto, de 01:00 a 03:00 hora local (D9). Se configura en el panel de la sede o en el central.
   - **No se empieza si Windows tiene un reinicio pendiente** (claves `RebootRequired` de Windows
     Update y `RebootPending` de CBS): se espera a la siguiente ventana. Así se evita que Windows
     Update reinicie en mitad de nuestra actualización; y si lo hace igualmente, el diario la retoma.
   - Las actualizaciones `security: true` y `severity: critical` pueden aplicarse fuera de la ventana
     **solo si no tocan `engine`**, porque no cortan grabación.
   - Comprobaciones previas: espacio libre (2× el tamaño de la versión + 1 GB) y que no haya otra
     instalación en curso (cerrojo compartido con el instalador).
5. **Respaldo** en `backups\pre-X.Y.Z-<fecha>\`:
   - `config\` y `users.json`.
   - `secrets\`: se copian los blobs cifrados con DPAPI de máquina, que siguen sirviendo en el mismo equipo.
   - Si el descriptor declara una migración de PostgreSQL **no reversible** y la base es local,
     también `pg_dump -Fc`. En las tiendas, la base está en la central: las migraciones de la central
     son responsabilidad de la publicación de la central, no de cada tienda (§2.8).
   - Número de cámaras grabando en ese momento (para el paso 7).
6. **Aplicar** (cada subpaso, anotado en el diario):
   1. `vmsctl services stop --only <afectados>`.
   2. `vmsctl version switch X.Y.Z` (escribe `active.json` con `trial: true`: **punto de compromiso**).
   3. `python -m vms config-migrate` (migración de `config.json`, §2.8, con `atomic_write`).
   4. `vms-migrate`, si toca.
   5. `vmsctl services start --only <afectados>`.
7. **Health check (120 s)** contra `GET /api/internal/health/deep` (token interno). Se exige todo:
   - `status != down` y `version == X.Y.Z`.
   - `engine.running` y `api_ok`.
   - **cámaras grabando ≥ las que grababan antes − 1**.
   - Si hay analítica: `analytics/status.json` con menos de 30 s y `running`.
   - El visor no cuenta.
8. **Resultado:**
   - **Fallo** → rollback automático:
     1. Parar los afectados y `vmsctl version switch <anterior>`.
     2. Restaurar el respaldo de `config\` (y `pg_dump` si se aplicó).
     3. Arrancar y repetir el health check.
     4. Informar `update_failed` con el motivo en el latido, en `public-status.json` y en la bandeja.
     5. Poner la versión en la **lista negra local**: no se reintenta hasta que haya otra mayor.
   - **Éxito** → `active.json` con `trial: false`, diario a `good`, se borran las versiones
     anteriores a N-1, se escribe `DisplayVersion` y `InstalledVersion` en el registro (§1.4) y se
     informa `update_ok`.
9. **Rollback manual.** Desde el panel central (por sede, vía la respuesta del latido) o con
   `vmsctl update rollback` elevado (bandeja, §2.3): «Volver a 2.0.0». Es la misma secuencia del paso
   8. Si se cambió la configuración después de actualizar, se avisa: «los cambios hechos desde el
   20-11 se perderán».
10. **Auto-actualización del actualizador (A/B):**
    - El nuevo `vms_updater` se escribe en la ranura inactiva y se cambia la ranura en `active.json`
      con `updater_trial: true`.
    - **`vmshost` (no el propio actualizador)** vuelve a la ranura anterior si el nuevo cae 3 veces
      en 10 min o si no confirma en 30 min. Como `vmshost` no se actualiza por esta vía, siempre
      queda alguien vivo para hacer la vuelta atrás.
11. **`vmshost` en sí** solo cambia con el instalador completo. Si hiciera falta un parche, se
    publica el instalador como `target` (`installers/`) y el actualizador lo ejecuta en silencio en la
    ventana, como caso excepcional y anotado.

**Visor:**
- Cuando cambia la versión activa, el backend emite el evento SSE `event: update` con
  `{"version":"2.1.0","viewer_restart":true}`.
- La carcasa (Rust) también vigila `public-status.json`.
- Las ventanas «Muro» se reabren desde la nueva versión al momento, unos 3 s.
- El «Panel» muestra un aviso: «Hay una versión nueva del visor. Se reiniciará cuando no lo estés
  usando», que se ejecuta tras 60 s sin actividad.

### 2.6 Red y firewall

- Las reglas son **por puerto y perfil**, no por programa: las rutas de los programas cambian con
  cada versión (`versions\X.Y.Z\...`).
  - TCP 8600 (solo si no hay HTTPS), TCP 8643 y UDP 8189 (+ TCP 8189 si está activado).
  - Perfil Privada (y Dominio, opcional). Nunca Pública.
  - Nombre de regla con prefijo `VMSMultimarca-` para borrarlas sin tocar otras.
- `vmsctl ports check` comprueba antes de instalar y antes de cada arranque:
  - que 8600, 8643, 8554, 8889, 8189, 9996, 9997 y 9998 estén libres (el actualizador ya no usa
    TCP: va por tubería, §2.2);
  - que **no** caigan en `excludedportrange` (Hyper-V/WSL), leído con la API `GetTcpTable2` /
    `CreatePersistentTcpPortReservation`, sin analizar texto de `netsh`.
  - Si choca, el asistente lo dice claro y ofrece cambiar el puerto, que se guarda en `.env`.

### 2.7 Formato del repositorio de actualizaciones y del manifiesto

**Estructura en R2** (TUF estándar, con prefijo de hash en los targets; `online/` y `offline/`
tienen la misma forma, §1.7):
```
online/metadata/1.root.json  2.root.json …  timestamp.json  snapshot.json  targets.json
targets/<sha256>.bundles/vms-2.1.0.json
targets/<sha256>.components/app/app-2.1.0.zip
targets/<sha256>.components/runtime/runtime-3.12.10-r4.zip
targets/<sha256>.components/engine/engine-1.21.1-r1.zip
targets/<sha256>.components/viewer/viewer-2.1.0.zip
targets/<sha256>.components/models/models-rfdetr-2026.10.zip
targets/<sha256>.components/updater/updater-2.1.0.zip
targets/<sha256>.channels/stable.json  channels/pilot.json
targets/<sha256>.installers/VMSMultimarca-Setup-2.1.0.exe
targets/<sha256>.data/advisories-20261005.json
```

**Entrada en `targets.json`** (campos `custom` permitidos por TUF):
```json
"bundles/vms-2.1.0.json": {
  "length": 2481,
  "hashes": {"sha256": "9f2c…"},
  "custom": {"kind": "bundle", "version": "2.1.0", "released": "2026-11-20T10:00:00Z"}
},
"components/app/app-2.1.0.zip": {
  "length": 8123456,
  "hashes": {"sha256": "1a7e…"},
  "custom": {"kind": "component", "component": "app", "version": "2.1.0"}
}
```

**Descriptor de versión `bundles/vms-2.1.0.json`** (versión de esquema 1, validado con pydantic en
`vms_updater/models.py`):
```json
{
  "schema": 1,
  "product": "vms-multimarca",
  "version": "2.1.0",
  "min_from": "2.0.0",
  "security": true,
  "severity": "high",
  "notes_es": "Corrige la autenticación Digest SHA-256 con cámaras Hikvision recientes.",
  "config_schema": 3,
  "db": {"central_migration": "0004", "store_requires_central_migration": "0004"},
  "requires": {"windows_build_min": 19045, "webview2_min": "130.0.0.0"},
  "authenticode": {"subject_o": "<razón social>", "subject_c": "ES",
                   "issuers": ["<CA de Certum que emite el certificado>"]},
  "components": {
    "runtime": {"version": "3.12.10-r4", "target": "components/runtime/runtime-3.12.10-r4.zip",
                "sha256": "…", "length": 74123456,
                "restart": ["VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral"]},
    "app":     {"version": "2.1.0", "target": "components/app/app-2.1.0.zip", "sha256": "…",
                "length": 8123456, "restart": ["VMSBackend", "VMSAnalytics", "VMSHeartbeat", "VMSCentral"]},
    "engine":  {"version": "1.21.1-r1", "target": "components/engine/engine-1.21.1-r1.zip",
                "sha256": "…", "length": 21456789, "restart": ["VMSEngine"], "recording_gap": true},
    "viewer":  {"version": "2.1.0", "target": "components/viewer/viewer-2.1.0.zip", "sha256": "…",
                "length": 6543210, "restart": []},
    "models":  {"version": "rfdetr-2026.10", "target": "components/models/models-rfdetr-2026.10.zip",
                "sha256": "…", "length": 98765432, "restart": ["VMSAnalytics"]},
    "updater": {"version": "2.1.0", "target": "components/updater/updater-2.1.0.zip", "sha256": "…",
                "length": 19876543, "restart": ["VMSUpdater"]}
  }
}
```

**Componente `data` (tabla de avisos de seguridad, CONTRATO §18.12).** No va en ningún descriptor de
versión ni reinicia nada. Entrada en `targets.json`:
`"data/advisories-20261005.json": {"length": …, "hashes": {"sha256": "…"}, "custom": {"kind": "data", "data": "advisories", "schema": 1, "generated_at": "2026-10-05T00:00:00Z"}}`.
En cada comprobación, `VMSUpdater` (con la misma verificación TUF que el resto) toma la entrada `data`
`advisories` con el `generated_at` más reciente; si es más nueva que la instalada, la descarga, valida el
esquema (`AdvisoryTable`, `schema` 1) y la escribe con `atomic_write` en `<datos>\ops\advisories\advisories.json`
(solo la escribe `VMSUpdater`). El backend elige entre esa y la que trae la versión
(`app\vms\ops\security\advisories.json`) la **válida con `generated_at` más reciente**. Se publica con
`python -m tools.release data advisories <archivo>` (firma de `targets`, como un canal).

**Canal `channels/stable.json`:**
```json
{"schema": 1, "channel": "stable", "version": "2.1.0", "paused": false,
 "updated": "2026-11-21T09:00:00Z"}
```

- Mover o pausar un canal es una publicación firmada más (con la YubiKey de `targets`):
  `python -m tools.release channel stable --version 2.1.0` o `--pause`.
- El panel central **no firma**. Solo cambia el canal de una sede entre los canales firmados,
  retiene sus actualizaciones o pide su rollback (§2.5 paso 2).
- Cada zip lleva dentro `MANIFEST.sha256` (hash por archivo), que se comprueba tras descomprimir. Es
  defensa en profundidad frente a zips manipulados en disco.
- Los zips son **reproducibles**: orden fijo, fecha `SOURCE_DATE_EPOCH`, sin metadatos de usuario y
  con los `.pyc` compilados en CI (§1.2). El alcance exacto de la reproducibilidad está en §4.1.

### 2.8 Migraciones y compatibilidad hacia atrás

- **`config.json`:**
  - `CONFIG_VERSION` pasa a 2 en la v2.0. Cadena de funciones puras
    `vms/core/config_migrations.py: MIGRATIONS = {1: v1_to_v2, 2: v2_to_v3, …}`, con pruebas por paso
    y fixtures `tests/core/fixtures/config_v1_*.json` sacadas de instalaciones reales anonimizadas.
  - **Regla:** una versión que encuentra `version` **mayor** que la suya **no guarda nunca**. Arranca
    en solo lectura con `config_warning` y `status: degraded`. El rollback restaura el respaldo, así
    que esto es solo una red de seguridad.
  - Los modelos persistentes pasan de `extra="ignore"` a `extra="allow"`: una versión N-1 conserva
    los campos que no conoce.
  - `Vendor` deja de ser `Literal` y pasa a `str` validado contra el registro (§3). Una marca
    desconocida, por ejemplo tras un rollback, **se conserva** con estado «Driver no disponible en
    esta versión» en lugar de desaparecer.
- **PostgreSQL (central):**
  - Patrón **expand/contract**: una versión N añade columnas o tablas sin romper la N-1; lo que se
    elimina se borra una versión después.
  - Cada migración declara en su cabecera `-- reversible: yes|no`. Si es `no`, `tools.release publish` obliga a
    marcarla en el descriptor y el rollback de la central restaura el `pg_dump`.
  - Nueva migración `0003_site_updates.sql`: tabla `site_versions` (sede, versión instalada, estado
    de la actualización, canal, retenida sí/no, ventana) para el panel central.

---

## 3. Sistema de drivers de fabricantes

### 3.1 Interfaz (`vms/vendors/registry.py` + `vms/core/interfaces.py`)

```python
@dataclass(frozen=True)
class StreamPreset:
    main: str                    # "/Streaming/Channels/{ch}01"
    sub: str | None              # "/Streaming/Channels/{ch}02"
    third: str | None = None
    rtsp_port: int = 554
    scheme: Literal["rtsp", "rtsps"] = "rtsp"
    query_safe: bool = True      # False si la ruta usa ?query (MediaMTX la descarta en rutas propias; ojo en el simulador)

class Capability(StrEnum):
    API_PROBE = "api_probe"            # leer modelo, serie, firmware
    API_CHANNELS = "api_channels"      # listar canales de NVR/DVR/XVR
    API_SNAPSHOT = "api_snapshot"
    API_CODEC_FIX = "api_codec_fix"    # poner el subflujo en H.264 con un clic (v2: Hik y Dahua)
    ONVIF = "onvif"
    DISCOVERY_WSD = "discovery_wsd"
    DISCOVERY_SADP = "discovery_sadp"
    DISCOVERY_DHIP = "discovery_dhip"
    ONVIF_MEDIA2 = "onvif_media2"      # Profile T: GetStreamUri de Media2, necesario para H.265 por ONVIF
    RTSPS = "rtsps"                    # v2.1
    MJPEG_HTTP = "mjpeg_http"          # v2.1
    EVENTS = "events"                  # fuera de la v2
    PTZ = "ptz"                        # fuera de la v2

@dataclass(frozen=True)
class DriverSpec:
    id: str                              # "hikvision", "dahua", "uniview", "tplink-vigi", …
    name: str                            # "Hikvision"
    brands: tuple[str, ...]              # ("Hikvision", "HiLook", "HiWatch", "LTS", "Annke")
    kinds: tuple[DeviceKind, ...]        # ("camera", "nvr", "dvr", "xvr")
    capabilities: frozenset[Capability]
    default_ports: dict[str, int]        # {"http": 80, "rtsp": 554, "onvif": 80}
    auth: tuple[Literal["digest-sha256", "digest-md5", "basic"], ...]
    lockout: LockoutPolicy | None        # (intentos, minutos) para no bloquear al usuario
    presets: Callable[[int, DeviceKind], StreamPreset] | None
    client: Callable[..., DeviceClient] | None   # None = solo RTSP/ONVIF
    detect: Callable[[DetectionHints], float]    # 0..1 a partir de scopes WSD, SADP, DHIP, cabecera HTTP, modelo
    notes_es: tuple[str, ...]            # avisos para el instalador («Desactiva Smart Coding…»)
    setup_hints_es: tuple[str, ...]      # pasos previos («Activa RTSP en la app Ezviz…»)
    maturity: Literal["verified", "fixtures", "community", "experimental"]

REGISTRY: dict[str, DriverSpec]          # se rellena en vms/vendors/drivers/<id>.py
def get_driver(vendor_id: str) -> DriverSpec | None
def best_match(hints: DetectionHints) -> list[tuple[DriverSpec, float]]
```

- `client_for`, `test_device` y `build_camera_sources` dejan de tener cadenas de `if` y consultan el
  registro. La firma de §5.1 del contrato no cambia.
- `rtsp.preset_paths(vendor, channel)` queda como fachada que delega en el registro (compatible).
- **Nueva API** `GET /api/vendors` (rol A): devuelve `[DriverSpec público]` (sin funciones). La
  interfaz construye a partir de ella el formulario de alta: marcas, puertos por defecto, avisos y
  si hace falta ruta manual. Se eliminan los presets duplicados en `panel.js`.
- **`maturity` es un dato visible**, no un adorno: sale en el alta, en `GET /api/vendors`, en la
  página «Acerca de» y en `docs/COMPATIBILIDAD.md`:
  - «Verificado con hardware»: hay una fila en `docs/COMPATIBILIDAD.md` con la prueba de 72 h
    superada (§4.7) para ese driver. **CI falla** si un driver dice `verified` sin esa fila
    (`tests/vendors/test_maturity_matches_matrix.py`).
  - «Probado con respuestas reales»: hay fixtures de un equipo real.
  - «Comunidad»: solo documentación pública.
  - «Experimental».
  - La v2 **no se anuncia** como «15 marcas verificadas». Se anuncia lo que diga la matriz el día de
    la publicación (p. ej. «2 verificadas, 5 probadas con respuestas reales, 8 por documentación»).
- **Identidad del equipo** (`DeviceIdentity`: número de serie y MAC, leídos por API, ONVIF o
  descubrimiento) se guarda en el alta. Sirve para encontrar el equipo si cambia de IP (§3.2,
  punto 11).

### 3.2 Correcciones de la auditoría y del informe de cámaras que entran en la v2

1. **Digest SHA-256 (RFC 7616) en `rtsp_probe.py`.**
   - Leer **todas** las cabeceras `WWW-Authenticate`.
   - Elegir la más fuerte que se soporte: SHA-256 > MD5 > Basic. Basic solo si el equipo tiene
     `allow_basic=true`.
   - Soportar `qop=auth` y `userhash`.
   - Prueba: servidor caótico con SHA-256, con varios retos, y con un reto que solo ofrece Basic.
2. **Un solo intento fallido por alta.**
   - Si el equipo devuelve 401 tanto en la API como en RTSP, no se reintenta.
   - Hay que distinguir «contraseña incorrecta» de «usuario bloqueado»: Hik ISAPI devuelve
     `userCheck`/`lockStatus` y Dahua devuelve el error `Locked`. Las respuestas exactas se fijan con
     fixtures reales.
   - El mensaje dice cuántos minutos esperar.
3. **`allow_basic` por equipo** (pendiente 3 de ESTADO.md): campo nuevo en `DeviceBase`, por
   defecto `false`, con aviso en la interfaz.
4. **`guess_vendor`** usa `best_match`: la puntuación por scopes WSD (`hardware/`, `name/`) pesa más
   que la del prefijo del modelo. `IPC-` deja de implicar Dahua (también lo usa Uniview).
5. **`DahuaClient.snapshot`** respeta `stream` (`snapshot.cgi?channel=N&type=1` para el subflujo,
   comprobado con fixture).
6. **Canales de DVR/XVR híbridos:** usar siempre el id que devuelve la API (ISAPI `InputProxy` y
   `Streaming/channels`; Dahua `LogicDeviceManager`), nunca suponer que el número de cámara coincide
   con el de canal. Fixture de un DVR Turbo HD con canales IP a partir de 33 (**no verificado** con
   hardware).
7. **GOP largo (H.264+/H.265+/Smart Codec):**
   - `test_device` avisa si el SDP o el primer fotograma tardan más de 4 s.
   - El muro amplía su tiempo de espera del primer fotograma a 12 s para esas cámaras (dato en
     `CameraOut.live.gop_hint`).
8. **«Corregir códec» con un clic** (Hik y Dahua, capacidad `API_CODEC_FIX`):
   - Si el subflujo no es H.264, la interfaz ofrece cambiarlo en el equipo y pide confirmación
     explícita.
   - Hik: `PUT /ISAPI/Streaming/channels/<id>02` con `videoCodecType=H.264`. Dahua:
     `configManager.cgi?action=setConfig&Encode[N].ExtraFormat[0].Video.Compression=H.264`.
   - **Antes de cambiar nada** se guarda la configuración actual del canal (`GET` del mismo recurso)
     en `config\device-backups\<equipo>\<fecha>.xml|.txt`.
   - La interfaz ofrece **«Deshacer»** (repone lo guardado con el mismo `PUT`/`setConfig`) durante
     30 días.
   - Queda en el **registro de auditoría**: quién (usuario), cuándo, equipo, canal, valor anterior y
     valor nuevo.
   - Las respuestas exactas se fijan con fixtures. **No verificado** con hardware.
9. **ONVIF Media2 (Profile T).** Con H.265 por ONVIF hace falta el servicio Media2
   (`tr2:GetProfiles` y `tr2:GetStreamUri`); el servicio Media1 no describe bien H.265. El driver
   `onvif` pregunta primero por Media2 (`GetServices`) y usa Media1 si no está. Fixtures de una
   cámara con Profile T y de una solo con Profile S.
10. **Límites de sesiones y ancho de banda de los NVR.** Al importar 16 canales de un NVR:
    - se pide el subflujo para la vista y el principal solo para grabar, una conexión por flujo
      (MediaMTX ya agrupa a los lectores);
    - si el NVR responde 453/503 o corta flujos, la interfaz lo explica («El grabador no admite más
      sesiones: conecta las cámaras directamente o baja la calidad») y no reintenta en bucle;
    - el alta muestra el ancho de banda estimado de entrada (suma de bitrates leídos por API).
11. **Cambio de IP por DHCP.** Si un equipo deja de responder y el descubrimiento (WSD, SADP, DHIP)
    encuentra **la misma serie o MAC** en otra IP, la interfaz propone «La cámara X ahora está en
    192.168.1.80: ¿actualizar?». Solo se cambia sola si el administrador activa «Seguir la IP
    automáticamente» para ese equipo. Además, la guía de alta recomienda IP fija o reserva DHCP.
12. **Firmware que vuelve a desactivar RTSP u ONVIF tras actualizarse.** Si un equipo que
    funcionaba pasa a rechazar la conexión RTSP (conexión rechazada en 554) o ONVIF, y su API
    responde con un firmware distinto al guardado, el estado dice «El equipo se actualizó
    (firmware A → B) y puede haber desactivado RTSP/ONVIF» con los pasos para reactivarlo
    (`setup_hints_es` del driver).

### 3.3 Pruebas de contrato por driver con fixtures

**Fixtures** en `tests/vendors/fixtures/<driver>/<modelo>__<firmware>/`:
```
meta.json            {"driver":"hikvision","model":"DS-7604NI-K1","firmware":"V4.30.085",
                      "kind":"nvr","captured":"2026-11-05","source":"Covert lab","anonymized":true,
                      "channels":4,"notes":"canal 3 offline, canal 4 H.265"}
http/0001.json       {"request":{"method":"GET","path":"/ISAPI/System/deviceInfo","headers":{…}},
                      "response":{"status":200,"headers":{…},"body":"<?xml …"}}
rtsp/main_ch1.sdp    SDP tal cual (anonimizado)
rtsp/handshake.json  OPTIONS/DESCRIBE con los retos WWW-Authenticate reales
onvif/*.xml          GetDeviceInformation, GetProfiles, GetStreamUri (si aplica)
discovery/*.bin|xml  respuestas WSD/SADP/DHIP
```

**Herramienta de captura** `python -m tools.capture_device --host 192.168.1.64 --driver hikvision
--user admin --out tests/vendors/fixtures/hikvision/`:
- Pide la contraseña por `getpass` y no la guarda.
- Hace **solo lecturas** (GET/DESCRIBE).
- **Anonimiza**: serie, MAC, IP, nombres de canal, `realm`/`nonce` sustituidos por valores
  deterministas y huellas de certificado.
- Se puede dar a Covert para que capture sus equipos sin instalar nada: va dentro del runtime.

**Batería común** `tests/vendors/contract/test_driver_contract.py`, parametrizada sobre registro ×
fixtures. Sirve las respuestas con un transporte httpx que reproduce las grabaciones y con un
servidor RTSP de reproducción. Para cada driver comprueba:
1. `probe()` → `DeviceInfo` con modelo, firmware y serie no vacíos.
2. `list_channels()` → canales igual a `meta.channels`, con estado y códec cuando la API los da.
3. Preset y canal → URL válida, credenciales codificadas `%XX`, sin `//` salvo que el driver lo declare.
4. Análisis del SDP → códec correcto (H264/H265/MJPEG), y que no falle si falta `rtpmap` o `sprop-*`.
5. Autenticación → elige el reto correcto; un 401 da **un** solo intento; bloqueo ≠ contraseña mala.
6. `snapshot()` → JPEG válido sin escribir en disco.
7. Detección → `best_match(hints de las fixtures)` pone este driver primero con más de 0,7.
8. Ningún texto de log contiene la contraseña: se comprueba con `caplog` y la contraseña de prueba
   `Sim#Pass:1@/x`.

Un driver con `maturity >= "fixtures"` **tiene que** tener al menos una carpeta de fixtures. CI
falla si no la tiene.

**Servidor RTSP caótico** `tools/mocks/rtsp_chaos.py` (asyncio, propio). Escenarios por nombre
(`?scenario=` en la ruta o una ruta por escenario):
- `digest-sha256`, `multi-challenge`, `basic-only`.
- `sdp-no-control`, `sdp-absolute-control`, `sdp-no-rtpmap`, `sdp-no-sprop`, `sdp-lf-only`.
- `double-slash`.
- `limit-2-sessions` (453), `busy-503`.
- `drop-without-keepalive`.
- `lockout-after-3` (simula el bloqueo de Hik/Dahua: 401 con mensaje de bloqueo).
- `slow-first-frame-10s`.

### 3.4 Matriz de marcas: qué entra en la v2

**Qué es cada cosa:** 3 drivers con API (Hikvision, Dahua, ONVIF genérico), 11 perfiles RTSP/ONVIF
(rutas y avisos por marca, sin hablar con la API del equipo) y 1 RTSP manual. Un perfil RTSP no es un
«driver verificado»: su madurez se lee en la columna de la matriz publicada.

**Requisito antes del piloto:** las filas P1 de **Hikvision y Dahua** (cámara + NVR + DVR/XVR)
terminadas **con hardware**: fixtures reales, 72 h y madurez `verified`. Sin eso no hay piloto.

| Prio | Driver (id) | Tipo en v2 | Capacidades v2 | Fixtures necesarias para la v2 | Después |
|---|---|---|---|---|---|
| P1 | `hikvision` (+HiLook, HiWatch, LTS, Annke) | **API ISAPI** | probe, canales, snapshot, codec-fix, WSD, **SADP (solo búsqueda)** | cámara serie 2 (fw 5.7+), NVR 4 ch, **DVR híbrido** | eventos ISAPI (v2.2) |
| P1 | `ezviz` | perfil RTSP | preset `/ch1/main` y `/h264/ch1/main/av_stream`, aviso «activa RTSP en la app; contraseña = código de verificación» | C6N | — |
| P1 | `dahua` (+Amcrest, Lorex) | **API CGI** | probe, canales, snapshot (con `stream`), codec-fix, WSD, **DHIP 37810** (adaptado de rroller/dahua, MIT, con atribución, limitado a la LAN y 5 paquetes/s) | cámara, NVR 4KS3, **XVR** | RPC2 completo, eventos |
| P1 | `imou` | perfil RTSP (ruta Dahua) | aviso «safety code» | una cámara Imou | — |
| P2 | `uniview` | perfil RTSP + ONVIF | `/unicast/c{ch}/s0/live` y `s1`; puerto 554 o 9090 | IPC2122 | LAPI (sin documentación pública) |
| P2 | `tplink-vigi` | perfil RTSP + ONVIF | `/stream1` y `/stream2`; aviso «desactiva Smart Coding» | C440 | — |
| P2 | `tapo` | perfil RTSP + ONVIF 2020 | aviso «crea una cuenta de cámara»; las de batería no tienen RTSP | C210 | — |
| P2 | `hanwha` | perfil RTSP + ONVIF | `/profile{N}/media.smp` | prestada (Q) | SUNAPI |
| P2 | `axis` | perfil RTSP + ONVIF | `/axis-media/media.amp?camera={ch}&videocodec=h264` | prestada (M10) | VAPIX (lib `axis`, MIT) |
| P2 | `milesight` | perfil RTSP + ONVIF | `/main`, `/sub`; probar con doble barra | — (documentación) | — |
| P2 | `ajax` | perfil RTSP 8554 + ONVIF | ruta pegada desde la app; aviso de puerto | prestada por Covert | — |
| P3 | `reolink` | perfil RTSP | `/Preview_01_main` y `_sub`; aviso «activa RTSP» | RLC-510A | API `api.cgi`, HTTP-FLV |
| P3 | `bosch` | perfil RTSP + ONVIF | `/?inst=1` e `inst=2` | — | RCP+ |
| — | `onvif` | **API ONVIF** (genérico) | GetDeviceInformation, GetServices, GetProfiles, GetStreamUri, GetSnapshotUri, **Media2 (Profile T) para H.265**; mejoras: perfil único, desfase de reloj (ajuste del `UsernameToken` con `GetSystemDateAndTime`), usuario ONVIF distinto | go2rtc ONVIF server + mocks | — |
| — | `generic` | RTSP manual | ruta a mano | — | — |
| v2.1 | `unifi` | RTSPS vía Protect | `rtsps://nvr:7441/<token>` | — | v2.1 |
| v2.1 | MJPEG HTTP | MediaMTX no lo ingiere de forma nativa (**no verificado**) | — | — | v2.1 vía go2rtc (MIT) |

**Fuera de la v2:** eventos y alarmas, PTZ, audio bidireccional, transcodificación.

**Política de H.265** (riesgo multimarca principal):
1. Subflujo en H.264: obligatorio para la vista en vivo. Aviso más «Corregir códec».
2. Pantalla completa (principal) en H.265:
   - Si la prueba S1 demuestra que WebView2 reproduce HEVC por WebRTC con decodificación por
     hardware en el PC de destino, se usa.
   - Si no, la pantalla completa muestra el **subflujo** con la etiqueta «Calidad reducida (H.265 no
     compatible)».
3. Reproducción de grabaciones en H.265:
   - `<video>` con fMP4 HEVC si WebView2 lo decodifica (S1).
   - Si no, «Descargar MP4» para un reproductor externo.
   - Transcodificar en la exportación (FFmpeg LGPL **sin** x264/x265, con codificadores por hardware
     o OpenH264 BSD) **queda para la v2.1**, después de revisar licencias.

---

## 4. Estrategia de pruebas

### 4.1 Capas y criterios

| Capa | Dónde | Orden | Criterio |
|---|---|---|---|
| Unitarias Python | `tests/**` existentes + nuevas | `pytest -m "not e2e and not slow"` | verde en Linux, macOS y Windows |
| Unitarias Rust | `native/*/src` | `cargo test --workspace` | verde; `clippy -D warnings` |
| Contrato por driver | `tests/vendors/contract/` | `pytest tests/vendors/contract` | todos los drivers × fixtures verdes; cobertura de `vms/vendors` ≥ 85 % |
| Integración con simuladores | `tests/vendors/`, `tests/engine/`, nuevo `tests/compat/` | `pytest -m "needs_mediamtx"` | matriz §4.2 verde |
| Actualizador (lógica) | `tests/updater/` | `pytest tests/updater` | repositorio TUF local temporal; ataques §4.4 rechazados |
| e2e macOS (actual) | `tests/e2e/`, `python -m tests.e2e.system_check` | igual que hoy | 10/10 pasos |
| e2e Windows (CI) | `tests/windows/` | `python -m tests.windows.run_e2e` | §4.6 completo |
| Corte de luz (laboratorio) | `tests/windows/powercut/` en una VM Hyper-V | `python -m tests.windows.powercut` | §4.4 bis |
| Hardware real | `docs/CHECKLIST-PRUEBAS.md` + `docs/COMPATIBILIDAD.md` | manual | §4.7 |

**Calidad transversal:**
- `ruff` y `mypy --strict` en `vms/core`, `vms/vendors` y `updater` (el resto con `mypy` normal).
- `pytest-cov` con informe en CI. Cobertura mínima global del 80 % (hoy no se mide). Se sube en
  bloques sin bajar nunca.
- Se añade **`pytest-randomly`** (orden aleatorio) para detectar pruebas que dependen del orden.
  Licencia **por verificar** antes de añadirlo.
- Las pruebas que se omiten tienen que aparecer en el resumen de CI. En CI de Windows no se acepta
  ninguna omisión «por falta de herramienta»: el workflow instala MediaMTX, ffmpeg de pruebas,
  Chromium y pwsh.
- **Textos sin voseo (comprobable, no opinable):** `tests/test_spanish_style.py` busca en
  `vms/web/`, `native/viewer/ui/`, `distribution/installer/lang/` y `docs/` una lista de formas
  prohibidas (`vos`, `tenés`, `querés`, `podés`, `sabés`, `hacé`, `mirá`, `instalá`,
  `ingresá`, `fijate`, etc., con límites de palabra y lista de excepciones; este plan y el propio
  test, que citan las formas prohibidas, están exceptuados). Falla el CI si aparece
  alguna. La revisión humana del tono sigue, pero ya no es el criterio de «terminado».
- **Reproducibilidad (alcance realista):**
  - **Se exige** que el payload Python y web (zips de `app`, `runtime`, `models`, `updater`) salga
    con el mismo SHA-256 en dos builds limpias: `.pyc` compilados en CI con `unchecked-hash`,
    `SOURCE_DATE_EPOCH`, orden fijo, pip con `--no-compile` en la instalación de `site-packages`.
  - **Se intenta** con los binarios Rust (`/Brepro` en el enlazador MSVC, `--remap-path-prefix`,
    `SOURCE_DATE_EPOCH`, toolchain fijado en `rust-toolchain.toml`). Si no salen idénticos, el
    criterio pasa a ser la atestación de procedencia de GitHub, y se documenta.
  - El instalador de Inno no se exige reproducible (incluye fechas propias): se comprueba que su
    payload sea el mismo que el reproducible.

### 4.2 Matriz de integración con simuladores (`tests/compat/`)

Herramientas de laboratorio, **nunca en el payload** (`tests/test_licenses.py` comprueba el payload
de `dist/`):
- MediaMTX como origen.
- ffmpeg de pruebas como publicador: en CI de Windows, una build fijada con SHA-256 solo para el
  runner.
- `rtsp_chaos`.
- Mocks Hik/Dahua/ONVIF.
- `tools/camsim` ampliado con estos perfiles.

| Caso | Cómo se simula | Qué se comprueba |
|---|---|---|
| H.264 baseline / high con B-frames | ffmpeg `-profile:v` | graba, `/list`, muro WebRTC con fotogramas |
| **H.265** principal + H.264 subflujo | ffmpeg `libx265` (herramienta local) | graba H.265; `codec_warning` correcto; pantalla completa → subflujo con etiqueta; reproducción → «Descargar» si no hay decodificador |
| **H.265 en el subflujo** | ídem | aviso + oferta de «Corregir códec» (mock Hik/Dahua acepta el PUT) |
| **MJPEG** por RTSP | ffmpeg `mjpeg` | MediaMTX lo acepta; el muro lo marca «no compatible con WebRTC» (comportamiento esperado y documentado) |
| Audio PCMA / AAC / sin audio | ffmpeg | graba; el muro no se rompe por la pista de audio |
| GOP 10 s (H.265+) | `-g 150` | el primer fotograma llega en menos de 12 s; aviso `gop_hint` |
| **Digest MD5 / SHA-256 / Basic** | MediaMTX `rtspAuthMethods` + `rtsp_chaos` | prueba de conexión correcta; Basic solo con `allow_basic` |
| Solo TCP / solo UDP | MediaMTX `rtspTransports` | `rtspTransport` de la cámara se respeta |
| **NVR multicanal** (16 ch, 2 offline) | camsim `--hikvision nvr1:16` + mock ISAPI | importación de canales, estados, 16 rutas, grabación |
| **XVR** (4 analógicos + 4 IP desde el 5) | mock Dahua con `LogicDeviceManager` de un XVR (fixture) | ids de canal correctos |
| **DVR híbrido Hik** (IP desde el 33) | mock ISAPI con fixture | ids 33-36 → rutas `3301`… |
| Bloqueo de usuario | `rtsp_chaos lockout-after-3` + mock ISAPI con bloqueo | 1 intento; mensaje «bloqueado N min» |
| Límite de sesiones | `limit-2-sessions` | MediaMTX abre **una** conexión por flujo aunque haya 4 muros |
| Corte y vuelta del equipo | camsim `off/on` | reconexión en menos de 10 s; muro «Sin señal» → vídeo |
| ONVIF: perfil único, reloj desfasado ±2 h, ONVIF desactivado, usuario distinto | mocks ONVIF ampliados | mensajes claros; desfase corregido |
| Descubrimiento WSD / SADP / DHIP | respondedores propios en `tools/mocks/` | marca detectada con más de 0,7; DHIP limitado (5 paquetes/s) |
| **ONVIF Media2 / Profile T** con H.265 | mock ONVIF con `tr2:` y otro solo con Media1 | Media2 preferido; URL H.265 correcta; caída a Media1 sin error |
| **NVR al límite** (16 canales, el NVR acepta solo 8 sesiones o corta por ancho de banda) | camsim `--max-sessions 8` + `rtsp_chaos busy-503` | mensaje claro, sin reintentos en bucle, las 8 que entran graban |
| **Cambio de IP por DHCP** | camsim cambia de IP y el respondedor WSD/SADP anuncia la misma serie | propuesta «ahora está en …»; con «seguir la IP» activado, vuelve a grabar sola en < 2 min |
| **Firmware que desactiva RTSP** | mock ISAPI con firmware nuevo + puerto 554 cerrado | estado «el equipo se actualizó…» con los pasos de `setup_hints_es` |
| **«Corregir códec» y deshacer** | mock Hik/Dahua que registra los `PUT` | copia `GET` antes del `PUT`; «Deshacer» repone el valor exacto; entrada en la auditoría |

### 4.3 e2e en macOS

Se mantienen `tests/e2e/*` y `system_check` con `VMS_ENGINE_MODE=child`. Se añade un paso «modo
attach»: el motor arranca aparte con `python -m vms engine-run`, que hace de `vmsctl` en desarrollo;
se reinicia el backend y se comprueba que **la grabación no tiene huecos** (spans de `/list`
continuos). Otro paso: **backend parado + motor reiniciado** → el motor vuelve a grabar solo, desde
su YAML, sin el backend.

### 4.4 Pruebas del actualizador (sin Windows, `tests/updater/`)

Repositorio TUF temporal generado con `tools/release/tuf_repo.py` y servido con un servidor HTTP
local.

| Caso | Esperado |
|---|---|
| Actualización 2.0.0 → 2.1.0 de app (repositorio simulado, servicios simulados) | descarga solo `app`; respaldo; switch; health ok; marca buena |
| Target con hash alterado | rechazado (`LengthOrHashMismatchError`); nada cambia |
| `targets.json` firmado con una clave no autorizada | rechazado |
| Ataque de versión anterior (snapshot o targets más antiguos) | rechazado por TUF |
| `timestamp` caducado (metadatos congelados) | no actualiza; informa `metadata_expired` |
| Reloj local desfasado | **no aplica**; informa `clock_skew`; no hay bucle de errores; la caducidad nunca se ignora |
| `root` rotado (1.root → 2.root con 2 de 3) | el cliente lo sigue |
| Descriptor con `min_from` mayor que la instalada | no aplica; informa |
| Health check falla | rollback; versión en lista negra; `update_failed` |
| Disco lleno a mitad de la descarga | limpia `.tmp`; reintenta en el ciclo siguiente |
| **Fallo inyectado en cada estado del diario** (`VMS_UPDATER_FAULT_AT=<estado>` mata el proceso justo antes y justo después de cada paso) | al arrancar, el diario retoma o revierte; el resultado final es siempre «nueva buena» o «anterior buena», nunca un estado intermedio; ejecutar dos veces da lo mismo (idempotente) |
| `active.json` ausente o corrupto | `vmshost` lo reconstruye con la última versión buena del diario |
| Actualizador nuevo que no arranca (ranura B rota) | `vmshost` vuelve a la ranura A en < 10 min; `update_failed` |
| Reinicio pendiente de Windows | no empieza; espera a la siguiente ventana |
| Espejo `file://` (repositorio `offline`) | mismo resultado que HTTP |
| USB con `offline-timestamp` de más de 60 días | no aplica; `metadata_expired`; mensaje «prepara un USB nuevo» |
| Ensayo de compromiso: clave `targets` comprometida, rotada con 2 de 3 llaves `root` | el cliente rechaza lo firmado con la clave vieja y acepta lo nuevo |
| Firma Authenticode con certificado renovado (sujeto igual, huella distinta) y rollback a la versión firmada con el anterior | ambas aceptadas |
| Setup antiguo ejecutado sobre una versión más nueva instalada por el actualizador | el instalador se niega (lógica de `InitializeSetup` probada con el doble de registro) |
| Migraciones de `config.json` v1 → v2 → v3 | igual a las fixtures esperadas; rechazo de guardado con versión mayor |

### 4.4 bis Cortes de luz de verdad (laboratorio, VM Hyper-V)

Matar el proceso no prueba lo que pasa con la caché de disco. En el PC Windows del laboratorio, con
una VM Hyper-V (Windows 11, instantánea limpia):
1. Instalar 2.0.0 con cámaras simuladas grabando.
2. Lanzar la actualización a 2.0.1 con `VMS_UPDATER_PAUSE_AT=<estado>` (el actualizador espera 30 s
   en ese estado y lo anuncia en `public-status.json`).
3. Desde el anfitrión, `Stop-VM -TurnOff` (equivale a quitar el cable).
4. Arrancar la VM y esperar 10 min. Comprobar: hay versión activa, los servicios arrancan, la
   grabación vuelve, `config.json` y `active.json` son JSON válidos y el diario termina en `good` o
   `rolled_back`.
5. Repetir para **cada estado** del diario (unos 10), 3 veces cada uno.

Lo ejecuta `python -m tests.windows.powercut` desde el anfitrión. Es requisito antes del piloto.

### 4.5 Pruebas del instalador y de `vmshost`/`vmsctl` sin Windows

`cargo test` con abstracciones de `ServiceManager`, `Firewall` y `Acl` (dobles en memoria): orden de
operaciones, idempotencia y códigos de salida. Análisis estático del `.iss` con `ISCC /Qp` (solo
en Windows/CI).

### 4.6 e2e en Windows por CI (`.github/workflows/e2e-windows.yml`, `tests/windows/`)

En `windows-latest` (el runner es administrador). Pasos, cada uno es una prueba pytest con su
registro:

1. **Preparar:**
   - Descargar los artefactos de `build.yml`: `Setup-2.0.0-ci.exe`, más un **repositorio TUF de
     prueba** con 2.0.1 (cambio de app), 2.0.2 (cambio de engine) y 2.0.3-roto (backend que no
     arranca).
   - Arrancar `camsim` (2 NVR × 4 canales, uno H.265) y un servidor HTTP que hace de R2 y Worker
     (`tests/windows/fake_update_server.py`).
2. **Instalación silenciosa:** `Setup.exe /VERYSILENT /TYPE=store /LOADINF=ci.inf /SECRETS=ci-secrets.json /LOG=…`. Código 0.
3. **Comprobaciones del sistema:**
   - Con `Get-Service`/`sc qc` vía `subprocess`: servicios con su cuenta virtual `NT SERVICE\…`,
     inicio automático y recuperación.
   - Con la API `GetNamedSecurityInfo`: ACL de `ProgramData\VMSMultimarca` (un usuario normal creado
     en la prueba **no** puede leer `recordings\`).
   - Firewall: reglas con prefijo y solo en Privada.
   - `bin\vmshost.exe` como `ImagePath` de todos los servicios y `state\active.json` válido.
   - Firma Authenticode de todos los PE nuestros (con `WinVerifyTrust`). En CI sin certificado se usa
     uno de prueba autoimportado en la raíz de confianza del runner.
4. **Arranque:** `vmsctl health wait --deep`. Alta de los 2 NVR por la API, 8 cámaras grabando antes
   de 60 s.
5. **Visor:**
   - Lanzar `VMS.exe --walls` con `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`
     (solo en builds de prueba).
   - Conectar con Playwright `connect_over_cdp`.
   - Comprobar:
     - páginas `/wall/1` (y 2 si el runner da un segundo monitor virtual: **no verificado**; si no, 1);
     - `video.requestVideoFrameCallback` cuenta más de 30 fotogramas por celda en 10 s;
     - el muro entró con el token de kiosco del grupo `VMS Operadores` (sin pantalla de login), y un usuario fuera del grupo no puede leer `kiosk.token`;
     - `GET /api/auth/me` → kiosk.
   - Bandeja: existencia del icono con UI Automation (`pywinauto`, BSD-3), opcional.
6. **Actualización v2.0.0 → 2.0.1 (app):**
   - `vmsctl update check` (elevado, por la tubería) y ventana de mantenimiento forzada para CI.
   - Un usuario normal (no administrador) intenta abrir la tubería → acceso denegado.
   - Esperar `update_ok`.
   - **Medir el corte:** spans de `/api/recordings/{cam}/timeline` alrededor de la actualización →
     hueco máximo **≤ 1 s** (tolerancia por la parte fMP4 en curso).
   - El visor sigue con fotogramas o se recupera en menos de 5 s.
7. **Actualización 2.0.1 → 2.0.2 (engine):** hueco máximo **≤ 15 s**; las rutas vuelven solas desde el YAML.
   Además: con `VMSBackend` parado, reiniciar `VMSEngine` → las 8 cámaras vuelven a grabar en < 30 s.
8. **Rollback:**
   - Publicar 2.0.3-roto → `update_failed`.
   - `active.json` vuelve a 2.0.2 y `DisplayVersion` del registro dice 2.0.2.
   - `config.json` idéntico al respaldo (hash).
   - Cámaras grabando como antes.
   - 2.0.3 en la lista negra (una segunda comprobación no la reintenta).
9. **Seguridad del actualizador en Windows:**
   - Un `.exe` con firma Authenticode inválida dentro de un target con hash correcto (repositorio de
     prueba firmado con la clave de CI, pero con el binario alterado antes de firmar el target)
     → **rechazado** por la segunda capa.
   - El servicio sigue en 2.0.2.
   - Fallo inyectado en cada estado del diario (`VMS_UPDATER_FAULT_AT`), con reinicio de servicios
     entre medias: termina siempre en una versión buena.
   - Ranura del actualizador rota → `vmshost` vuelve a la anterior.
   - `Setup-2.0.0-ci.exe` ejecutado con 2.0.2 instalada → se niega a bajar de versión.
10. **Reinicio simulado:** `Restart-Computer` no es viable en el runner. Se sustituye por
    `vmsctl services stop` + `start` y por matar `mediamtx.exe` y `python.exe`. Vuelven solos en
    menos de 10 s.
11. **Desinstalación:**
    - `unins000.exe /VERYSILENT`: servicios, reglas y `Program Files\VMSMultimarca` eliminados; `ProgramData` conservado
      (grabaciones).
    - Segunda pasada con `/PURGE`: todo eliminado.
    - Reinstalar encima de los datos conservados: arranca con la configuración anterior.
12. **Artefactos de la ejecución:** registros de servicios, `diag bundle`, capturas del visor y
    `results-windows.json` (el mismo formato que `tests/e2e/RESULTADOS.md`).

Duración estimada: 35-50 min (**estimación**). Se ejecuta en `main` y en tags, no en cada PR.

### 4.7 Pruebas con hardware real (checklist)

Se amplía `docs/CHECKLIST-PRUEBAS.md` y se crea `docs/COMPATIBILIDAD.md`, la **matriz publicada**.
Columnas:

> marca · modelo · tipo · firmware · fecha · descubrir · alta y prueba · canales · directo subflujo ·
> directo principal (códec) · grabar 24 h · reproducir · descargar · snapshot · codec-fix ·
> reconexión tras reinicio · cambio de IP (DHCP, misma serie) · bloqueo (1 intento) · límite de
> sesiones (NVR) · ONVIF Media2 · RTSP/ONVIF tras actualizar el firmware · 72 h · resultado

Una marca solo pasa a «Verificado» (`maturity = verified`) con su fila completa y las 72 h superadas
(§3.1).

Checklist por sesión de laboratorio:
- [ ] **S1 (puerta de decisión, primera semana):** en el PC Windows 11 con GPU, el visor Tauri
  mínimo con 4 ventanas × 16 subflujos H.264 640×360 a 15 fps durante 30 min. Medir:
  - CPU y GPU;
  - decodificador real (`chrome://media-internals` dentro del WebView: «D3D11VideoDecoder» o
    «MediaFoundation» = hardware);
  - fotogramas perdidos.
  - Además: HEVC por WebRTC y HEVC fMP4 en `<video>`.
  - **Aprobado:** CPU < 60 %, hardware activo y < 1 % de fotogramas perdidos. **Si no:** Electron
    (plan B) o bajar la resolución de los subflujos.
- [ ] **S5 (fijación del certificado del visor remoto):** opciones A y B de §2.3 contra un backend
  con certificado autofirmado: certificado cambiado (debe avisar), redirección 302 a otra ruta del
  mismo servidor (#4575), reconexión tras reiniciar el backend. Veredicto escrito.
- [ ] Instalar con el asistente en Windows 11 limpio y, si se decide admitirlo (D11), en Windows 10 22H2 con ESU (sin Python ni nada previo),
  con un usuario con tilde (`José`) y la carpeta de grabaciones en `D:\Grabaciones CCTV`.
- [ ] Reinicio real del PC con muros abiertos: servicios arriba, muros abiertos solos con el inicio
  de sesión automático de la cuenta de muros (el asistente lo configura si se pide; contraseña en
  LSA con `vmsctl autologon`, **no verificado**).
- [ ] Windows Update con reinicio a mitad de grabación: hueco medido. Y con un reinicio pendiente
  de Windows Update al llegar la ventana: el actualizador no empieza.
- [ ] Cortes de luz en cada paso de la actualización (§4.4 bis).
- [ ] Recarga en caliente del YAML con 16 cámaras grabando: se añade una cámara 17 y se cambia la
  contraseña de la 3 → solo se reinician esas rutas (las demás sin hueco en `/list`).
- [ ] Actualización real desde R2 en la ventana nocturna, con 1 Hik + 1 Dahua: hueco medido.
- [ ] Cada equipo de la lista de compra (§7.3): fila completa en la matriz y **captura de fixtures**
  con `tools.capture_device`.
- [ ] 72 h de prueba con 16 cámaras: memoria de MediaMTX y del backend (`pprof` de MediaMTX en
  127.0.0.1 solo durante la prueba), reconexiones y disco.
- [ ] Equipos del cliente: probar una actualización de firmware real en una Hik y una Dahua y
  comprobar qué pasa con RTSP/ONVIF después.
- [ ] Antivirus: Defender con las reglas ASR por defecto + 1 EDR comercial si Covert usa alguno. Sin
  cuarentenas.

---

## 5. Pendientes de ESTADO.md que entran en la v2

| Pendiente (ESTADO.md o auditoría) | Solución v2 | Bloque | Prueba |
|---|---|---|---|
| 1. Servicios con cuenta virtual | §2.2 (`NT SERVICE\…`, ACL por SID, DPAPI de máquina + ACL) | B1 | e2e Windows paso 3 |
| 2. HTTPS desde el instalador | Casilla del asistente → `vmsctl tls setup` (`python -m vms tls-cert` + regla 8643 + importación opcional en la raíz local); el visor remoto usa fijación de huella | B3 (+B1 para `vmsctl tls`) | e2e Windows: `https://127.0.0.1:8643/api/health` |
| 3. Permitir Basic por equipo | `allow_basic` en `DeviceBase` + selección de reto §3.2 | B5 | `rtsp_chaos basic-only` |
| 4a. Reconexión del muro en 21-29 s | `whep.js`: evento SSE `engine` (`restarted`) que pone el contador a 0; tope de 2 s cuando el fallo es 404/503 del motor; con el motor como servicio aparte, un reinicio del backend ya no corta | B2 | `tests/web`: motor matado → vídeo en **≤ 6 s** |
| 4b. Hilos de OpenVINO en el mini PC | `VMS_ANALYTICS_OV_THREADS` + valor por defecto según núcleos físicos, medido en el N150 | B4 (pequeño, va con models) | checklist E |
| Posible fuga de memoria de MediaMTX | prueba de 72 h con `pprof` (§4.7) | laboratorio | informe en `tests/e2e/RESULTADOS.md` |
| Prueba que depende de la hora (`tests/fakes.py:69`) | grabaciones simuladas «hoy a mediodía en la zona de la sede» | fase 0 | ejecutar con `faketime`/`time-machine` a las 00:30 CEST |
| Sin ruff/mypy en el venv | añadir a `requirements-dev` y al CI | fase 0 | CI |
| Sin cobertura | `pytest-cov` | fase 0 | CI |
| Rutas de Homebrew fijas en pruebas | `shutil.which` + variables `VMS_TEST_FFMPEG`; nunca `/opt/homebrew` en código del producto (`system_check.py:58` incluido) | fase 0 | CI Windows y Linux sin omisiones |
| Puertos fijos y rangos excluidos | `vmsctl ports check` + puertos configurables en el asistente | B1/B3 | prueba con un puerto ocupado a propósito |
| Perfil de red pública | el asistente lo detecta y ofrece cambiarlo a Privada (con confirmación) | B3 | e2e: red marcada como Pública → aviso |
| Kiosco tras Windows Update | inicio de sesión automático opcional + muros en `HKLM\Run --walls` | B2/B3 | checklist |
| Instalación que descarga de Internet | payload cerrado y firmado (§1.2, §2.4) | B1/B3 | e2e con el runner sin red durante la instalación (regla de firewall de salida) |
| `install.ps1` / WinSW | se mantienen **solo** para la v1 instalada; `vmsctl migrate-from-v1` convierte una instalación de la v1 (servicios WinSW → `vmshost`, datos intactos) | B1/B3 | e2e: instalar la v1 con `install.ps1` → instalar el Setup 2.0 encima |
| Windows 10 22H2 sin soporte desde el 14-10-2025 | política D11 (solo con ESU) + aviso del instalador | B3 | checklist |

---

## 6. Reparto del trabajo

### 6.1 Fase 0 (arquitecto, antes de paralelizar; 5-6 días)

Sin esto, los bloques se pisarían. Entregables:
1. **CONTRATO.md v2.0**, secciones nuevas:
   - §13 disposición en disco, servicios, `vmshost`, `active.json` y `journal.json` (formato y
     estados), `atomic_write`.
   - §14 CLI de `vmsctl` (órdenes, flags, códigos de salida, JSON).
   - §15 actualizador (tubería `\\.\pipe\VMSMultimarca.updater` y sus mensajes, `public-status.json`,
     formato de §2.7, repositorios `online`/`offline`).
   - §16 registro de drivers (§3.1), `maturity`, `DeviceIdentity` y `GET /api/vendors`.
   - §17 visor: token de kiosco por archivo, fijación de certificado (tras S5) y eventos SSE
     `update`/`engine`.

   Cambios en §4.3/§5.2: modo `attach` y **el YAML como fuente única de rutas** (tras S2).
2. **Esqueletos compartidos** (todos los cambios a archivos compartidos que se conocen hoy se hacen
   **aquí**, para que luego nadie tenga que tocarlos):
   - `vms/core/interfaces.py` (`DriverSpec`, `Capability`, `DetectionHints`, `DeviceIdentity`).
   - `vms/core/models.py` (`Vendor = str` validado, `extra="allow"`, `CONFIG_VERSION = 2`,
     `allow_basic`, `identity`, `follow_ip`).
   - `vms/core/atomic.py` (`atomic_write`) y su gemelo en `native/common/`.
   - `vms/api/events.py` con los eventos SSE `engine` y `update` ya declarados (vacíos).
   - `vms/api/app.py` con `include_router` de los routers nuevos vacíos (`vendors`, `updates`, `local`).
   - `vms/web/static/js/devices.js` separado de `panel.js` (cambio mecánico).
   - Contenedores vacíos en `index.html` (`#device-form-root`) y `status.html` (`#updates-root`).
3. **Infraestructura de pruebas:**
   - Arreglo de la prueba que depende de la hora.
   - `ruff`, `mypy`, `pytest-cov` y `pytest-randomly` en `requirements-dev` (licencias verificadas).
   - `.github/workflows/ci.yml` con **un job por bloque ya creado** (vacío), para que cada bloque
     rellene el suyo sin tocar el resto (el archivo es de B3).
   - `tests/fixtures/redaction_vectors.json` y `tests/test_spanish_style.py` (voseo).
4. **Pruebas de concepto con veredicto escrito** (`docs/investigacion-v2/spikes.md`):
   - **S1**: WebView2 y decodificación por hardware (§4.7, necesita el PC Windows: **requiere al
     usuario**). **Puerta de decisión: B2 no arranca hasta tener el veredicto.**
   - **S2**: MediaMTX v1.21.1 con el YAML como fuente única: recarga por ruta (solo se reinician
     las que cambian), renombrado atómico que dispara la recarga, grabación sin el backend. En macOS.
   - **S3**: firma TUF `root`/`targets` ECDSA P-256 con YubiKey vía PKCS#11 (`HSMSigner`) y
     verificación con python-tuf 7 (si no hay YubiKey, con SoftHSM y se repite al comprarlas).
   - **S4**: `windows-service-rs` + `vmshost` mínimo + Job Object + cuenta virtual en
     `windows-latest` (un servicio «hola mundo» con puntero y vuelta atrás).
   - **S5**: fijación del certificado en el visor (opciones A y B de §2.3). Puede ir en paralelo a
     B2 cuando S1 haya pasado.
5. Estructura de carpetas nueva creada (vacía, con `LEEME.md` por carpeta y su dueño).

### 6.2 Bloques en paralelo

**Reglas comunes a todos los bloques:**
- Cada bloque **solo** edita sus carpetas y archivos.
- **Cada archivo compartido tiene un único dueño** (tabla de abajo). Quien necesite un cambio en un
  archivo que no es suyo **se lo pide al dueño** (nota en CONTRATO §12 con el cambio exacto), y el
  dueño lo hace. Nadie edita un archivo ajeno «porque es solo una línea».
- Ninguno hace commit ni push. Ninguno instala nada global.
- Lo que no pueda probar sin Windows se entrega con validación estática y queda en la lista del e2e
  de Windows.
- «Terminado» = criterios de abajo + `pytest` completo verde + revisión cruzada (§6.3).

**Dueños de los archivos compartidos:**

| Archivo | Dueño | Quién le pide cambios |
|---|---|---|
| `.github/workflows/*.yml` | B3 | B1 (job de servicios), B2 (build del visor), B4 (`publish-meta.yml`, `timestamp.yml`), B5 (job de compatibilidad), B6 (job `b6-operacion`). Cada bloque ya tiene su job en `ci.yml` |
| `native/Cargo.toml` (workspace) y `native/common/` | B1 | B2 |
| `vms/core/models.py`, `vms/core/interfaces.py` | B5 (tras la fase 0) | B1, B4, B6 |
| `vms/core/config_migrations.py` | B4 | B5 (si cambia un modelo persistente, entrega la migración como petición) |
| `vms/api/events.py` | **arquitecto hasta que B2 arranque** (veredicto de S1); después, B2 | B1, B4 y B6. Los 6 eventos de la v2 (`V2_EVENTS`, sus `TypedDict` y sus `publish_*`) ya están declarados y **congelados**: emitirlos no exige tocar el archivo. Un evento o campo nuevo se pide por CONTRATO §12 |
| `vms/core/settings.py` | arquitecto | todos. Ya declara `VMS_ENGINE_MODE` (B1), `VMS_UPDATE_SOURCE` (B4) y los `VMS_LLM_*` (B6); una variable nueva se pide por CONTRATO §12 |
| `vms/core/heartbeat_extras.py`, `central/agent.py`, `central/heartbeat.py`, `vms/api/state.py` | arquitecto | nadie en el caso normal: el latido ya recoge `update` (B4, `vms_updater/heartbeat.py`) y `health` y `evidence_key` (B6, `vms/ops/heartbeat.py`); cada bloque solo escribe su proveedor en su carpeta (CONTRATO §7.3 bis) |
| `vms/__main__.py` | B1 (`engine-config`, `engine-run`) | B4 y B6 si necesitan una orden en `python -m vms` (lo normal es su propio `python -m vms_updater …` o `python -m vms.ops.evidence …`) |
| `docs/TERCEROS.md`, `THIRD_PARTY_NOTICES.txt`, `deploy/third_party_notices.py`, `tests/test_licenses.py`, `native/deny.toml` | arquitecto | B5 (`rroller/dahua`), B6 (Driver.js) y B2 (crates del visor): la petición trae origen, versión o commit, licencia y texto del aviso |
| `vms/api/routes/analytics.py`, `vms/api/routes/system.py` | arquitecto | B6 (los ganchos de ámbito ya están: CONTRATO §18.8) |
| `vms/api/app.py` | arquitecto (solo en la fase 0 y en la integración) | todos |
| `tests/windows/` (arnés, pasos 1-5 y 10-12) | B3 | — |
| `tests/windows/test_update_*.py` (pasos 6-9) | B4 | B3 le da el arnés |
| `tests/fixtures/redaction_vectors.json` | B1 | B5 (nuevos formatos de URL de marca) |
| `docs/CONTRATO.md` | arquitecto | todos (por nota en §12) |
| `vms/api/routes/__init__.py`, `central/app.py`, `central/extensions.py` | arquitecto (los routers de todos ya están registrados desde la fase 0) | todos |
| `vms/api/routes/cameras.py`, `live.py`, `recordings.py`, `walls.py` | arquitecto (integración) | B2, B5, B6 |
| `vms/api/permissions.py`, `vms/api/routes/users.py` | B6 | B2 (filtrar el SSE `status` por ámbito) |
| `vms/web/pages/*.html` | arquitecto: cada bloque ya tiene su contenedor y su módulo JS (CONTRATO §18.9) | todos |
| `vms/web/static/js/panel.js`, `devices.js` | B5 | B6 (estados vacíos) |
| `vms/web/static/js/ui.js`, `api.js`, `css/app.css` | arquitecto | todos |
| `pyproject.toml`, `requirements-*.txt` (locks) | arquitecto | todos (con la licencia verificada) |
| `tests/conftest.py`, `tests/fakes.py` | arquitecto | todos (los dobles nuevos de un bloque van en su carpeta de pruebas) |

#### B1 — Plataforma Windows: arrancador, servicios, runtime y motor como servicio

| | |
|---|---|
| **Carpetas propias** | `native/vmshost/`, `native/vmsctl/`, `native/common/`, `native/Cargo.toml`, `distribution/runtime/`, `vms/engine/` (modo `attach`, YAML como fuente única, `logtail.py`), `vms/core/winsec.py` (nuevo), `vms/core/atomic.py`, `vms/core/credentials.py` (solo el backend DPAPI), `tests/engine/test_attach_*.py`, `tests/core/test_winsec.py`, `tests/core/test_atomic.py`, `tests/fixtures/redaction_vectors.json` |
| **Entradas** | CONTRATO §13-§14 (fase 0); `install.ps1` actual como referencia funcional; S2 y S4 |
| **Salidas** | `vmshost.exe` (puntero, vigilancia de la versión a prueba, reconstrucción de `active.json`); `vmsctl.exe` (todas las órdenes de §1.3); `python -m distribution.runtime.build --out build/runtime` (runtime win_amd64 con `.pyc` compilados y `MANIFEST.sha256`); modo `VMS_ENGINE_MODE=attach` con YAML atómico; `python -m vms engine-config` y `python -m vms engine-run` (desarrollo); DPAPI de máquina + ACL con migración desde la v1; `vmsctl migrate-from-v1` |
| **Terminado cuando** | (1) `cargo test` + `clippy` verdes, con pruebas de los dobles de SCM, firewall y ACL, y de `vmshost` (puntero ausente o corrupto, hijo que cae 3 veces, versión a prueba que no se confirma); (2) `pytest tests/engine` verde en modo `child` **y** `attach`; (3) prueba macOS: backend reiniciado 5 veces con el motor aparte → **0 huecos** en `/list`, y motor reiniciado con el backend parado → vuelve a grabar; (4) en `windows-latest` (job de B1 en `ci.yml`): `vmsctl services install --role store` con un payload mínimo → servicios con `NT SERVICE\…` y `ImagePath` en `vmshost`, `vmsctl health wait` = 0, `services uninstall` limpio; (5) los vectores de redacción pasan en pytest y en cargo; (6) `atomic_write` con prueba de fallo inyectado entre escribir y renombrar |

#### B2 — App de escritorio (visor) y experiencia en vivo — **arranca tras el veredicto de S1**

| | |
|---|---|
| **Carpetas propias** | `native/viewer/` (Tauri: `src-tauri/`, `ui/` con las páginas locales), `vms/api/routes/local.py`, `vms/api/events.py`, `vms/web/static/js/wall.js`, `whep.js`, `banner.js` (nuevo), `vms/web/static/css/wall.css`, `vms/web/pages/wall.html`, `tests/web/test_wall_*.py`, `tests/viewer/` |
| **Entradas** | CONTRATO §17; S1 (umbral y decisión Tauri/Electron); S5 (fijación del certificado); estado del motor que expone B1 |
| **Salidas** | `VMS.exe` (bandeja, panel, muros por monitor, instancia única, inicio con sesión, lista de servidores con fijación de certificado, páginas locales de conexión y diagnóstico); entrada de muros con `kiosk.token`; reconexión rápida; aviso de actualización; rollback desde la bandeja con elevación; modo de prueba con CDP |
| **Terminado cuando** | (1) `cargo test` del visor (asignación de monitores con tabla de casos: 1→4 monitores, desconexión y DPI distintos); (2) `tests/web`: motor matado → vídeo en ≤ 6 s y backend reiniciado → muros sin login; (3) un usuario fuera de `VMS Operadores` no puede leer `kiosk.token` y su visor muestra «sin permiso para abrir muros»; (4) build `cargo tauri build --no-bundle` en `windows-latest` y prueba de humo por CDP (una ventana muro con fotogramas); (5) capacidades de Tauri revisadas: sin IPC para orígenes remotos (prueba que lo intenta desde la página del backend); (6) pruebas de S5 automatizadas (certificado cambiado → aviso y no carga) |

#### B3 — Instalador gráfico, CI de Windows y e2e de Windows

| | |
|---|---|
| **Carpetas propias** | `distribution/installer/` (`VMSMultimarca.iss`, `pascal/*.pas`, `lang/`, `assets/` con iconos y banner), `distribution/layout.py` (monta `versions\X.Y.Z` desde los artefactos), `tools/build/` (`all`, `sign`, `sbom`), `.github/workflows/` (todos), `tests/windows/` (arnés y pasos 1-5, 10-12), `docs/INSTALACION-WINDOWS.md` (reescrita para el asistente) |
| **Entradas** | Salidas de B1 (`vmshost.exe`, `vmsctl.exe`, runtime) y B2 (`VMS.exe`). Mientras no estén: **dobles** (un `vmsctl.exe` de prueba que registra las llamadas, construido desde `native/vmsctl` con `--features fake`, y un visor de prueba que solo abre una ventana) |
| **Salidas** | `VMSMultimarca-Setup-X.Y.Z.exe`; instalación silenciosa con `/LOADINF` + `/SECRETS`; negativa a bajar de versión; desinstalador con conservar/`/PURGE`; e2e de Windows completo (§4.6 pasos 1-5 y 10-12; los pasos 6-9 los escribe B4 sobre este arnés); aviso de Windows 10 |
| **Terminado cuando** | (1) `build.yml` produce el instalador desde un commit limpio; dos builds dan el **mismo SHA-256 del payload Python/web** (alcance de §4.1); (2) e2e pasos 1-5 y 10-12 verdes en `windows-latest`; (3) instalar el Setup 2.0 encima de una v1 de `install.ps1` conserva configuración y grabaciones; (4) el asistente tiene capturas de cada página en los artefactos de CI y `tests/test_spanish_style.py` pasa sobre `distribution/installer/lang/`; (5) `tools.build all` funciona igual en un Windows sin GitHub (documentado en `docs/EMPAQUETADO.md`) |

#### B4 — Actualizaciones, publicación y migraciones

| | |
|---|---|
| **Carpetas propias** | `updater/` (paquete `vms_updater`: `client.py`, `journal.py`, `apply.py`, `health.py`, `models.py`, `control_pipe.py`), `tools/release/` (`tuf_repo.py`, `publish.py`, `channel.py`, `mirror.py`, `site_token.py`), `infra/update-worker/`, `vms/core/config_migrations.py`, `vms/api/routes/updates.py`, `vms/web/static/js/updates.js`, `central/updates.py` + vistas del panel central para versiones, `vms/db/migrations/0003_site_updates.sql`, `tests/updater/`, `tests/windows/test_update_*.py`, `tests/windows/powercut/`, `tests/core/test_config_migrations.py`, `docs/PUBLICAR-VERSION.md` |
| **Entradas** | CONTRATO §13 y §15; formato §2.7; `vmsctl version switch` y `services` (B1; mientras tanto, un doble Python con la misma CLI); arnés de `tests/windows/` (B3); S3 |
| **Salidas** | Servicio actualizador con diario y A/B; repositorios TUF `online` y `offline`, y herramientas de publicación con firma local por YubiKey; Worker multicliente; panel central con canal por sede, retener y rollback; componente `data` (tabla de avisos, §2.7) instalado en `<datos>\ops\advisories\`; proveedor `update` del latido (`vms_updater/heartbeat.py`); `/status` con el estado de la actualización; migraciones de configuración; descriptor y canales; prueba de corte de luz |
| **Terminado cuando** | (1) todos los casos de §4.4 verdes; (2) e2e de Windows pasos 6-9 verdes sobre el arnés de B3; (3) `python -m tools.release publish --dry-run` genera un repositorio que `ngclient` valida, con `root`/`targets` ECDSA P-256 (SoftHSM en CI); (4) prueba del Worker con Miniflare (sin token → 401; token revocado → 401; token de otro cliente → 403; metadatos públicos); (5) `docs/PUBLICAR-VERSION.md`: publicar, pausar, revertir, preparar USB y **«Si roban una llave»**, ensayado una vez de verdad; (6) §4.4 bis ejecutado en la VM Hyper-V con el resultado en `tests/e2e/RESULTADOS.md` |

#### B5 — Drivers multimarca y compatibilidad

| | |
|---|---|
| **Carpetas propias** | `vms/vendors/` (incluidos `registry.py` y `drivers/<id>.py`), `vms/core/rtsp.py` (fachada), `vms/core/models.py` e `interfaces.py` (tras la fase 0), `vms/api/routes/vendors.py`, `vms/web/static/js/devices.js`, `tools/mocks/` (incluidos `rtsp_chaos.py`, `sadp.py`, `dhip.py`, ONVIF Media2), `tools/capture_device/`, `tools/camsim/` (perfiles nuevos), `tests/vendors/` (`contract/`, `fixtures/`), `tests/compat/`, `docs/COMPATIBILIDAD.md`, `docs/ALTA-EQUIPOS.md` |
| **Entradas** | CONTRATO §16; informe de cámaras; fixtures reales cuando lleguen del laboratorio o de Covert |
| **Salidas** | Registro con 3 drivers con API, 11 perfiles y RTSP manual (§3.4); correcciones de §3.2 (incluidas Media2, límites de NVR, cambio de IP, firmware y «Corregir códec» con copia, deshacer y auditoría); descubrimiento SADP y DHIP; batería de contrato; servidor caótico; herramienta de captura; matriz publicada con la madurez real |
| **Terminado cuando** | (1) añadir un driver nuevo = **un archivo** `drivers/<id>.py` + fixtures (se demuestra añadiendo `milesight` en una sola PR); (2) batería de contrato y matriz §4.2 verdes; (3) `grep` sin presets duplicados en JS; (4) prueba: alta con contraseña incorrecta → exactamente **1** petición con credenciales al equipo (contador del mock); (5) `rroller/dahua` adaptado con atribución en el archivo y en `docs/TERCEROS.md`, y `tests/test_licenses.py` sin cambios en rojo; (6) `test_maturity_matches_matrix.py` verde. **Para el piloto, además:** Hikvision y Dahua en `verified` (con hardware, §3.4) |

#### B6 — Operación, IA de verificación y onboarding

Fuente: [`investigacion-v2/funciones-ia-onboarding.md`](investigacion-v2/funciones-ia-onboarding.md),
prioridades 1-13 (las 14-21 quedan para la v2.1 o si Covert las pide). Contrato: CONTRATO §18.

| | |
|---|---|
| **Carpetas propias** | `vms/ops/` (salud de imagen, informe, hora, previsión, evidencias y marcadores, avisos, diagnóstico, auditoría de seguridad con `security/advisories.json`, almacén `ops.sqlite3`), `vms/api/permissions.py`, `vms/api/routes/` `health.py`, `evidence.py`, `notifications.py`, `diagnostics.py`, `security_audit.py`, `timeline.py`, `onboarding.py`, `counts.py` y `users.py`, `vms/web/static/js/` `onboarding.js`, `help.js`, `health.js`, `security.js`, `notifications.js`, `timeline-events.js`, `bookmarks.js`, `evidence.js`, `counts-export.js`, `vms/web/static/css/ops.css`, `vms/web/static/help/`, `vms/web/vendor/driver.js/` (Driver.js 1.9.0, MIT, con su LICENSE), `central/ops.py` y sus vistas, `tests/ops/`, la sección «Salud de cámara» de `docs/RGPD-EIPD.md` |
| **Entradas** | CONTRATO §18; capacidades `time_read` y `security_read` de B5 (mientras no lleguen, dobles en `tests/ops/`); `winsec` de B1 para guardar la clave de firma de evidencias con DPAPI; eventos SSE ya declarados (§17.3); la tabla de avisos la publica B4 como componente TUF `data` |
| **Salidas** | Prioridades 1-13: (1) salud/sabotaje 0-100 con OpenCV clásico y causas; (2) informe de salud por tienda + vista central «tiendas con problemas hoy» + CSV; (3) desfase horario cámara↔PC; (4) previsión de días de grabación con simulador; (5) exportación de evidencias con SHA-256, manifiesto firmado Ed25519, acta y visor HTML portátil con marca de agua (no quemada); (6) marcadores con bloqueo de retención; (7) avisos por correo y webhook con agrupación; (8) CSV de conteos; (9) «¿por qué no conecta?» por reglas (LLM opcional solo para redactar); (10) onboarding: asistente de primer uso, «?» contextual, estados vacíos y recorridos con Driver.js; (11) auditoría de seguridad con la tabla de avisos propia + KEV/NVD; (12) permisos por cámara; (13) línea de tiempo con eventos |
| **Terminado cuando** | (1) batería de imágenes **sintéticas** (generadas en la prueba, nunca fotos de personas) para cada causa con la causa y la puntuación esperadas, y 50 fotogramas normales sin falsos positivos; < 20 ms por comprobación a 640 px; histéresis probada; (2) desfase con mocks ONVIF/ISAPI/CGI a ±2 h y con `timeMode` manual; (3) previsión con un disco simulado; (4) `python -m vms.ops.evidence verify` acepta el paquete y falla si se cambia un byte; `visor.html` abre sin red en Chromium y detecta el cambio; (5) un tramo protegido sobrevive al borrado de MediaMTX y caduca; (6) correo con un servidor SMTP simulado y webhook con firma HMAC verificada; avisos agrupados; (7) diagnóstico: cada regla con su mock y exactamente **1** intento con credenciales; (8) auditoría: la tabla valida su esquema, casos Hikvision (fecha de build) y Dahua (versión), RTSP anónimo detectado con `rtsp_chaos`, sin salir a Internet; (9) asistente completo con Playwright y nunca en `/wall/N`; (10) operador con ámbito: 404 en cámaras fuera de su ámbito (vivo, grabación, descarga, reglas de analítica), no las ve en `/api/status` ni puede añadirlas a un muro (422), y el kiosco igual que antes; (11) informe y latido con `payload.health`; vista central y CSV; (12) RGPD: tras una ejecución completa, `ops\` solo contiene referencias de cámara y metadatos; EIPD actualizada; (13) `pytest` completo y `test_spanish_style.py` verdes; licencias revisadas (`tests/test_licenses.py`) |

### 6.3 Integración y revisión

- **Revisión cruzada:** B1↔B3 (Windows), B2↔B5 (interfaz), B4↔B1 (servicios, puntero y diario),
  B6↔B5 (capacidades `time_read`/`security_read` y diagnóstico), B6↔B2 (interfaz, SSE y kiosco).
  Además, un **revisor de seguridad independiente** sobre `updater/`, `vmshost`, la tubería de
  control, `kiosk.token`, `vmsctl acl/firewall`, `tools/release`, el Worker y lo sensible de B6 (firma de
  evidencias, webhook, permisos por cámara y credenciales temporales de la auditoría), con el mismo método de
  la v1: cada hallazgo tiene que ir con una prueba que lo reproduce.
- **Semana de integración:**
  - e2e de Windows completo en verde 3 veces seguidas (para descartar fallos intermitentes).
  - `system_check` macOS 10/10.
  - Actualizar `ESTADO.md` y `CONTRATO.md` §12.
- **Orden de fusión:** fase 0 → B5 y B1 (no dependen de nadie) → B6 → B2 → B4 → B3 (el que integra).

---

## 7. Riesgos, costes y decisiones del usuario

### 7.1 Riesgos

| Riesgo | Prob. | Impacto | Mitigación |
|---|---|---|---|
| WebView2 no decodifica 4×16 flujos por hardware | media | alto | S1 en la semana 1 y B2 no arranca sin veredicto; plan B Electron; subflujos de menor resolución en 4×4 |
| H.265 en WebView2 (WebRTC y `<video>`) | alta | medio | política de §3.4; «Corregir códec» con deshacer; ONVIF Media2; transcodificación en v2.1 |
| Corte de luz a mitad de actualizar | media (147 tiendas) | crítico | puntero atómico + diario + `vmshost`; fallo inyectado en CI; cortes reales en VM Hyper-V (§4.4 bis) |
| Actualizador nuevo que no arranca | baja | alto | `vmshost` fijo vuelve a la ranura anterior; acciones de recuperación del SCM; el instalador repara |
| Robo de la cuenta de GitHub | baja | crítico | `targets` en llave física y firmado en local; CI solo tiene `snapshot`/`timestamp`; procedimiento de compromiso ensayado |
| Perder llaves TUF | baja | crítico | `root` 2 de 3 (2 YubiKey + papel en caja fuerte); ensayo de rotación en CI |
| Renovación del certificado rompe el rollback | alta si no se diseña | alto | sello de tiempo RFC 3161; comprobación por sujeto y CA, no por huella (§1.6) |
| Windows Update reinicia durante la ventana | media | medio | no empezar con reinicio pendiente; el diario retoma tras el reinicio |
| SmartScreen avisa hasta que la firma gana reputación | alta al principio | medio | firma desde el primer piloto; instrucciones para el instalador de Covert |
| Antivirus o EDR ponen en cuarentena DLL de wheels | media | alto | firmar todo PE sin firma; probar con Defender ASR y el EDR de Covert; contacto de falsos positivos de Microsoft |
| La recarga del YAML de MediaMTX reinicia más rutas de las que debe | baja-media | alto | S2 con v1.21.1; prueba de 16 cámaras en el laboratorio; alternativa: recarga de rutas desde copia cifrada en `vmsctl` |
| Contraseñas de cámaras en el YAML en disco | — (aceptado) | medio | ACL estricta; fuera de diagnósticos y respaldos en claro (§2.5) |
| Reloj de la tienda desfasado | media | medio | no se actualiza y se avisa; comprobación de NTP en `vmsctl diag` |
| Tienda sin Internet con USB caducado | media | bajo | repositorio `offline` de 60 días; mensaje claro |
| Actualización defectuosa en 147 tiendas | baja | crítico | canal `pilot` (3 tiendas) → `stable` por tandas de sedes; health check con cámaras grabando; rollback automático; pausa de canal |
| Fuga entre clientes en el servidor de actualizaciones | baja | alto | el panel no tiene credenciales de Cloudflare; tokens con prefijo por cliente (§1.7) |
| Promesa de compatibilidad que no se cumple | media | alto | madurez visible; «verificado» solo con 72 h; Hik y Dahua verificados antes del piloto |
| La fixture no refleja todos los firmwares | alta | medio | matriz con firmware; captura sencilla para Covert; aviso de firmware que desactiva RTSP |
| Windows 10 en tiendas | media | medio | D11; aviso del instalador |
| Tiempo: 5 bloques a la vez | media | medio | la fase 0 fija contratos, dobles y dueños de archivos; B3 integra al final; el hardware marca el calendario |

### 7.2 Costes recurrentes y de entrada (aproximados, sin IVA)

| Concepto | Coste | Estado |
|---|---|---|
| Firma Authenticode | **Certum OV desde 209 €** por certificado (≤ 459 días). Artifact Signing (~9,99 USD/mes) solo si la empresa tiene 3 años o más | precio de Certum, de su tienda; requisito de 3 años, [Q&A de Microsoft](https://learn.microsoft.com/en-us/answers/questions/5972282/does-azure-artifact-signing-still-require-3-years) |
| Licencia comercial de Inno Setup | pago único con 2 años de actualizaciones | precio **no verificado** (no sale en [la página](https://jrsoftware.org/isorder.php)) |
| Cloudflare R2 + Workers | **0-5 €/mes** con 150 sedes (10 GB y 100 000 peticiones/día gratis) | precios de R2 verificados en el informe; uso **estimado** |
| GitHub privado + Actions | 0-30 €/mes según los minutos de Windows (el e2e completo solo en `main` y tags) | **no verificado** |
| 2 llaves YubiKey 5 (+ papel y sobre para la caja fuerte) | ~110-180 € una vez | **no verificado** |
| PC del laboratorio con Hyper-V | 0 € si ya es Windows 11 Pro; si es Home, la actualización a Pro | precio **no verificado** |
| Windows 10 con ESU (lo paga el dueño de los PC, no Unmanned) | 61 USD/PC el año 1 + 122 USD/PC el año 2 (desde el 14-10-2026), acumulativo | fuentes secundarias (p. ej. [Ingite](https://ingite.com/windows-10-esu-price-doubles-october-2026/)): **no verificado** con Microsoft |
| Dominio para actualizaciones (`updates.<dominio>`) | 0 € si usas un dominio que ya tienes | — |
| Laboratorio de hardware (lista §7.3) | **1.500-2.500 €** si se compra todo (estimación gruesa, **no verificada**); mucho menos con préstamos de Covert | **no verificado** |

### 7.3 Laboratorio mínimo para la v2

En orden de prioridad (modelos orientativos del informe de cámaras). **Lo marcado como «antes del
piloto» no es opcional.**
1. **Antes del piloto:** NVR Hikvision de 4 canales PoE + cámara Hikvision serie 2 con firmware nuevo.
2. **Antes del piloto:** NVR Dahua + cámara Dahua.
3. **Antes del piloto:** DVR Hikvision Turbo HD híbrido y XVR Dahua (con 1 cámara analógica).
4. Ezviz C6N, Imou, Uniview IPC2122, VIGI C440, Tapo C210, Reolink RLC-510A.
5. Préstamo de Covert: Ajax, Axis y Hanwha, y una cámara con ONVIF Profile T.
6. PC Windows 11 **Pro** con 4 salidas y GPU (para S1, S5 y las pruebas de corte de luz con Hyper-V)
   y un mini PC N150 (analítica).

### 7.4 Decisiones que necesitan al usuario (dinero o cuentas)

| # | Decisión | Opciones | Recomendación | Bloquea |
|---|---|---|---|---|
| **D1** | Proveedor de firma de código | Certum OV (209 €+) / Artifact Signing (solo sociedad con ≥ 3 años) | **Certum OV** a nombre de la empresa, salvo que Unmanned Studio sea sociedad con 3 años o más. Hace falta saber la forma jurídica y la antigüedad | Piloto (en CI se usa un certificado de prueba) |
| **D2** | Repositorio privado en GitHub con Actions | GitHub (Free/Team) / sin GitHub (scripts en el PC Windows) | GitHub privado, con 2FA por llave física en la cuenta | B3 (se puede empezar sin él) |
| **D3** | Licencia comercial de Inno Setup | comprar / no comprar (no es estrictamente obligatoria) / NSIS | Comprar: es un pago único y deja limpia la posición comercial | Distribución comercial |
| **D4** | Llaves de publicación: comprar 2 YubiKey y decidir quién guarda la segunda y dónde va la copia en papel | YubiKey A (tú) + YubiKey B (otra persona de confianza) + papel en caja fuerte | Así. Si la B también la guardas tú, el sistema funciona, pero no protege contra que te roben a ti las dos | Primera publicación real |
| **D5** | Cuenta de Cloudflare (R2 + Workers) y dominio para `updates.` | Cloudflare / otro S3 | Cloudflare | B4 (en pruebas basta Miniflare) |
| **D6** | Compra o préstamo del laboratorio (§7.3) | comprar / pedir a Covert / mixto | Comprar Hik y Dahua (P1, obligatorio antes del piloto); pedir a Covert el resto | Matriz de compatibilidad, fixtures reales y piloto |
| **D7** | Política de H.265 ante el cliente | exigir H.264 en el subflujo (con «Corregir códec») / pagar la transcodificación (v2.1) | Exigir H.264 en el subflujo en v2 | Mensajes de la interfaz |
| **D8** | ¿Algún cliente exige MSI (GPO/Intune)? | no / sí → WiX (con el OSMF) | No por ahora | Nada en v2 |
| **D9** | Ventana de mantenimiento y quién aprueba pasar de `pilot` a `stable` | 01:00-03:00; aprobación de Unmanned + Covert | Así | B4 |
| **D10** | Tiendas piloto de la v2 | 3 tiendas de Covert | — | Despliegue |
| **D11** | ¿Se admite Windows 10? | solo Windows 11 / Windows 10 con ESU de pago | **Solo Windows 11** para equipos nuevos; Windows 10 solo si el cliente paga ESU, y como «sin garantía de funciones nuevas» | Textos del instalador y laboratorio |
| **D12** | PC del laboratorio con Hyper-V | usar uno con Windows 11 Pro / actualizar a Pro / VMware Workstation | Windows 11 Pro con Hyper-V | Pruebas de corte de luz (§4.4 bis) |

### 7.5 Calendario orientativo

| Semana | Trabajo |
|---|---|
| 1 | Fase 0 + S1 en el PC Windows (con el usuario) + S2-S4 + D1, D2, D5, D6 |
| 2-5 | B1, B3, B4 y B5 en paralelo desde el inicio; B2 (y S5) en cuanto S1 da el veredicto. B3 trabaja con dobles hasta tener `vmshost`, `vmsctl` y `VMS.exe` |
| 6 | Integración, e2e de Windows 3 veces en verde y revisión de seguridad |
| 6-8 | Laboratorio: Hik y Dahua a `verified` (72 h), cortes de luz en Hyper-V, matriz de compatibilidad, fixtures reales. Correcciones |
| 9 | Piloto en 3 tiendas por el canal `pilot`, con una actualización real (2.0.0 → 2.0.1) |

Son semanas de calendario y **dependen del hardware y de las decisiones D1-D6, D11 y D12**. El código
puede ir más rápido; validar con equipos reales no.

---

## 8. Crítica y respuesta (revisión del plan 1.0)

La revisión crítica del plan 1.0 encontró 22 puntos. Este es el resumen de qué se hizo con cada uno.

| # | Punto | Respuesta | Dónde |
|---|---|---|---|
| 1 | DPAPI-NG con `SID=` no sirve fuera de dominio | **Aceptado.** DPAPI de máquina + ACL al SID del servicio como diseño principal | §2.2 |
| 2 | YubiKey (HSMSigner) solo admite ECDSA | **Aceptado** y comprobado en el código: `root` y `targets` en ECDSA P-256 | §1.6 |
| 3 | La junction y los JSON no sobreviven a un corte de luz | **Aceptado.** Fuera las junctions: puntero `active.json` atómico, diario, `atomic_write` para todo, reconstrucción en `vmshost`, prueba con Hyper-V | §2.4, §2.5, §4.4 bis |
| 4 | Nadie vigila al actualizador | **Aceptado.** `vmshost` fijo (solo lo cambia el instalador) elige ranura y revierte | §1.3, §2.5 paso 10 |
| 5 | Con la cuenta de GitHub caen las dos firmas | **Aceptado.** `targets` y Authenticode en local con llave física; CI solo `snapshot`/`timestamp`; procedimiento de compromiso ensayado | §1.6, §1.8 |
| 6 | Artifact Signing pide 3 años | **Aceptado.** Certum OV pasa a ser la opción por defecto | §1.6, D1 |
| 7 | Fijar la huella rompe el rollback | **Aceptado.** Sello de tiempo RFC 3161 y comprobación por sujeto y CA | §1.6 |
| 8 | La grabación depende del backend en modo `attach` | **Aceptado, con un coste que se declara:** el YAML pasa a ser la fuente única de rutas, y eso pone las contraseñas de las cámaras en disco (protegidas por ACL). La recarga por ruta se ha leído en el código de MediaMTX (`main`) y se confirma con v1.21.1 en S2 | §2.5, S2 |
| 9 | La fijación del certificado del visor no está resuelta | **Aceptado.** Prueba S5 con dos opciones y criterio de salida | §2.3, §6.1 |
| 10 | Quién lee el token de `127.0.0.1:8610` | **Aceptado y ampliado:** fuera TCP; tubería con ACL de SYSTEM y Administradores. Además, como la bandeja corre con el token filtrado por UAC, el rollback desde la bandeja pide elevación | §2.2, §2.3 |
| 11 | El instalador puede bajar de versión en silencio | **Aceptado.** El actualizador escribe `DisplayVersion`; el instalador se niega a bajar | §1.4 |
| 12 | Windows Update reinicia en la ventana | **Aceptado en parte.** Se cambia la ventana a 01:00-03:00, no se empieza con un reinicio pendiente y el diario retoma. **No se acepta** fijar las horas activas de Windows por defecto: es tocar la política de Windows Update del cliente (y las horas activas cubren como máximo 18 h, así que siempre queda un hueco). Si Covert lo quiere, se hace por su GPO, no por nuestro instalador | §2.5 paso 4 |
| 13 | El USB no sirve con un `timestamp` de 7 días; con reloj malo no se puede ignorar la caducidad | **Aceptado.** Repositorio `offline` aparte de 60 días; con reloj desfasado no se aplica nada | §1.7, §2.5 paso 1 |
| 14 | El panel central vería los tokens de todos los clientes | **Aceptado.** El panel no tiene credenciales de Cloudflare; tokens por cliente dados de alta desde Unmanned | §1.7 |
| 15 | «15 drivers verificados» engaña | **Aceptado.** Madurez visible y comprobada por CI; Hik y Dahua verificados con hardware antes del piloto | §3.1, §3.4 |
| 16 | Faltan casos de compatibilidad | **Aceptado:** Media2/Profile T, límites de NVR, cambio de IP por DHCP con serie/MAC, firmware que desactiva RTSP/ONVIF | §3.2, §4.2, §4.7 |
| 17 | «Corregir códec» modifica equipos del cliente | **Aceptado.** Copia previa, deshacer 30 días y auditoría | §3.2 punto 8 |
| 18 | Sobreingeniería | **Aceptado en lo concreto:** fuera para la v2.1 el porcentaje por canal, la asignación firmada por el panel y el emparejamiento por tubería (los muros usan el token de kiosco por archivo con ACL, que es la misma frontera de confianza: el grupo `VMS Operadores`). Llaves: 2 YubiKey + papel, umbral 2. shawl sigue como plan B de `vmsctl run`. Se mantienen los lenguajes actuales: añadir `vmshost` no suma ninguno (es Rust, como `vmsctl` y el visor), el Worker es un archivo pequeño en TypeScript y el Pascal Script se limita a páginas del asistente que llaman a `vmsctl`. Lo que se gana con el token por archivo frente a la tubería es simplicidad; lo que se pierde es que un token copiado sirve hasta que se rota (`vmsctl kiosk rotate`) | §2.3, §2.5, §2.7, §1.6 |
| 19 | Bloques que se pisan | **Aceptado.** Tabla de dueños de archivos compartidos y regla de «se le pide al dueño»; los cambios conocidos a archivos compartidos se hacen en la fase 0 | §6.1, §6.2 |
| 20 | Criterios de «terminado» no verificables | **Aceptado.** `.pyc` en CI + `PYTHONDONTWRITEBYTECODE`; reproducibilidad exigida solo al payload Python/web y «se intenta» para Rust; voseo con lista de palabras en CI | §1.2, §4.1 |
| 21 | Windows 10 22H2 sin soporte | **Aceptado.** Decisión D11; solo con ESU | §1.4, D11 |
| 22 | B2 arranca sin el veredicto de S1 | **Aceptado.** B2 espera a S1; B3 sigue con un visor de prueba | §6.1, §6.2 |

**Lo que sigue sin verificar después de la revisión:** que la recarga por ruta de MediaMTX se
comporte igual en v1.21.1 que en `main` (S2); que la firma PKCS#11 con YubiKey funcione de punta a
punta con python-tuf 7 (S3); que Certum se pueda usar desde un script sin intervención (no bloquea:
se firma en local); el precio de la licencia de Inno; el precio de ESU (fuentes secundarias); la
hora exacta a la que Windows Update reinicia (por eso el diseño ya no depende de ella).

---

## 9. Decisiones de la noche del 5/10 y desviaciones de la fase 0

El responsable delegó las decisiones técnicas con tres límites: no gastar dinero, no crear cuentas
externas y no publicar fuera del repositorio privado. Lo que sigue **manda sobre el resto del plan**
donde choque.

### 9.1 Decisiones del responsable

| # | Decisión | Qué cambia | Hasta cuándo |
|---|---|---|---|
| N1 | **Sin certificado Authenticode todavía** | Las builds salen sin firmar. El hueco está preparado: directiva `SignTool=` del `.iss` vacía, paso `python -m tools.build sign` (jsign) que se salta si no hay certificado, y en el e2e de Windows un certificado de prueba autoimportado en el runner. SmartScreen avisará en el laboratorio (se documenta) | Hasta D1 |
| N2 | **Sin YubiKey** | Claves TUF de **desarrollo** en software o en SoftHSM, marcadas `dev` (archivos y etiquetas `dev-*`, `"x-vms-env": "dev"` en el `root`), **nunca en el repositorio** (`%USERPROFILE%\.vms-dev-keys\` o el token SoftHSM `vms-dev`). La ceremonia real (2 YubiKey + papel, umbral 2 de 3) queda escrita en `docs/PUBLICAR-VERSION.md` (B4) | Hasta D4 |
| N3 | **Sin Cloudflare** | El Worker se construye y se prueba con Miniflare. El actualizador admite, además del Worker, una **fuente HTTP estática** y un **espejo USB/carpeta** (`file://`) (CONTRATO §15.1). La prueba real de actualización usa un servidor HTTP local en CI | Hasta D5 |
| N4 | **S1 no bloquea la fase 0** | S1 necesita el PC Windows del usuario. Se sigue con Tauri y se deja el kit de S1 listo (visor mínimo + `s1.ps1` + guía de 3 pasos, `spikes/s1-webview2/`). **Electron sigue como plan B.** B2 no arranca hasta tener el veredicto | Veredicto de S1 |
| N5 | **Hyper-V y cortes de luz reales, al laboratorio** | §4.4 bis no se hace esta noche. Sí se hacen (B4) las pruebas del diario con fallos inyectados (`VMS_UPDATER_FAULT_AT`) | Laboratorio (D12) |
| N6 | **Bloque B6 nuevo** | «Operación, IA de verificación y onboarding» con las prioridades 1-13 del informe de funciones (§6.2 y CONTRATO §18), respetando sus descartes de licencia y de RGPD | — |

### 9.2 Decisiones técnicas tomadas en la fase 0

| Tema | Decisión | Motivo |
|---|---|---|
| `packaging/` | Se llama **`distribution/`** (`distribution/runtime/`, `distribution/installer/`, `distribution/layout.py`) | Con `__init__.py` (para `python -m …`), `packaging/` taparía el paquete `packaging` de PyPI que usan pip, pytest y `tools.lock_requirements` |
| Salud de imagen | Corre en el **backend** (no en la analítica): `numpy` y `opencv-python-headless` pasan al extra `[vms]` | Los puestos de control no tienen servicio de analítica y también necesitan la salud de imagen. El runtime ya incluye OpenCV |
| Tabla de avisos de seguridad | Viaja como componente TUF `data` (y dentro de cada versión), preparada en el PC de publicación con NVD + KEV + EPSS | Así va firmada como el resto del producto y ni las tiendas ni la central salen a Internet (el informe proponía que la firmara la central) |
| Marcadores protegidos | Enlaces duros a los segmentos (copia si cambia el volumen) en `evidence\protected\` | Proteger no duplica disco y la retención de MediaMTX no los borra |
| Permisos por cámara | Ganchos en las rutas desde la fase 0 (`vms/api/permissions.py`, sin efecto hasta B6) | B6 activa el ámbito sin tocar archivos de otros bloques |
| Puntero `active.json` | Solo lo escriben el actualizador (LocalSystem) y el instalador; un servicio sin privilegios pide la vuelta atrás con `state\rollback-request.json` | Hallazgo de S4: con el diseño de la prueba, todas las cuentas virtuales tendrían que poder escribir el puntero (CONTRATO §13.3, a cerrar por B1) |
| Retención en MediaMTX | `recordDeleteAfter` se queda en el YAML; cambiarla abre un segmento nuevo en todas las cámaras (≤ 1 GOP de hueco, medido en S2) y se avisa | Así la retención funciona aunque el backend esté caído días |
| Firma TUF por PKCS#11 | `HSMSigner` de securesystemslib 1.5.1 usa **python-pkcs11 (MIT)**, no PyKCS11 (GPL) | Comprobado en el código de la versión 1.5.1 (S3) |
| Calidad en CI | `ruff` (E4, E7, E9, F, B), `mypy --strict` en `vms/core` (ya pasa) y normal en `vms/ops`; el resto se suma por bloques. `pytest-randomly` activo por defecto; `pytest-cov` en CI con el **umbral del 80 % ya exigido** (`--cov-fail-under=80` en `pytest-ubuntu`; primera medida: 83,34 %). Se sube por bloques, nunca se baja | Que CI quede verde hoy sin rebajar nada que ya se cumpla |
| Licencias Rust | Además de MIT/BSD/Apache-2.0/ISC/Zlib/MPL-2.0 se aceptan **Unicode-3.0** (tablas Unicode que usan `regex` y 18 crates de Tauri), **0BSD**, **MIT-0** y **CC0-1.0** (más permisivas que MIT), y las expresiones `OR` que incluyan una permitida (p. ej. `r-efi`: MIT o Apache-2.0 o LGPL; se elige MIT). `native/deny.toml` lo fija y `cargo deny check licenses bans sources` corre en el job B1 | Son permisivas y sin copyleft; la lista literal del responsable no las nombraba. B2 amplía `deny.toml` (por petición) cuando el visor entre en `native/` |
| Tabla de avisos: dónde vive y cuál manda | La de la versión va dentro del código (`app\vms\ops\security\advisories.json`); la que llega por TUF entre versiones la escribe **solo `VMSUpdater`** con `atomic_write` en `<datos>\ops\advisories\advisories.json`. Manda la **válida con `generated_at` más reciente**; la de la versión es la base (CONTRATO §18.12, §2.7) | Revisión de la fase 0: B4 y B6 tocaban el mismo dato desde dos lados sin regla |
| Ganchos de ámbito por cámara | También en `PUT /api/walls/{monitor}` (cámaras que se añaden), listas y estado de analítica y `/api/status` | Revisión de la fase 0: un operador con ámbito podía poner en un muro una cámara ajena y verla en el kiosco |
| Latido y ajustes compartidos | `vms/core/heartbeat_extras.py` (proveedores por bloque) y `VMS_ENGINE_MODE`/`VMS_UPDATE_SOURCE` declarados en la fase 0 | Revisión de la fase 0: B1, B4 y B6 iban a editar los mismos archivos |
| CI en GitHub | `ci.yml` y `s1-kit.yml` en la rama `v2`, validados con actionlint y ejecutados en GitHub. El token de `gh` de este equipo no tiene el permiso `workflow` (GitHub rechaza subir flujos con él): los commits se subieron por SSH con la clave ya configurada en el equipo. Para que `gh` pueda tocar flujos: `gh auth refresh -h github.com -s workflow` | Sin ese permiso, cualquier cambio de un flujo se sube por SSH |

