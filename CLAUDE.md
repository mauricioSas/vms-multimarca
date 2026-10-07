# VMS Multimarca: contexto para Claude Code

Léelo entero antes de tocar nada. Es el traspaso de las sesiones anteriores, que se hicieron en la nube, a este PC
con Windows.

## El usuario

- Habla español y **no es programador**.
  - Respóndele en español sencillo, con pasos numerados y sin jerga.
  - Dale solo el resultado y lo que tiene que hacer él, en pocos pasos.
- Quiere que **lo dejes todo funcionando tú**, sin preguntarle cosas técnicas. Literal: «no puede ser que yo esté
  preguntando este tipo de cosas».
  - Decide tú lo razonable, pruébalo y publícalo.
  - Pregunta solo lo que de verdad depende de él: dinero, cuentas o datos de su red y sus cámaras.
- Si necesitas algo de su pantalla, pídele **una foto o una captura**. Si hay que ejecutar algo, dale **una sola
  línea** para pegar en PowerShell, diciéndole si tiene que abrirlo como administrador.

## Qué es el producto

Videovigilancia (VMS) para tiendas y oficinas con cámaras **Hikvision, Dahua y ONVIF** mezcladas, sin licencia por
cámara. Es una **aplicación de escritorio para Windows 11**: el usuario no quiere entrar por navegador.
Repositorio: https://github.com/mauricioSas/vms-multimarca. Es **público**, para tener minutos ilimitados de
GitHub Actions; el usuario no quiere pagar.

| Pieza | Dónde | Qué hace |
|---|---|---|
| Backend | `vms/` (Python 3.12, FastAPI, puerto 8600) | API y web: equipos, cámaras, muros, grabaciones, salud, diagnóstico, evidencias, avisos |
| Web | `vms/web/static/` (HTML, CSS y JS sin framework) | Panel: Equipos y monitores, Estado del sistema, Reproducción… |
| Motor de vídeo | MediaMTX (`tools/fetch_mediamtx.py` lo descarga) | Graba y sirve el vídeo; en Windows es el servicio `VMSEngine` en modo *attach* |
| Fabricantes | `vms/vendors/` | Drivers, descubrimiento (WS-Discovery, SADP de Hikvision, DHIP de Dahua), prueba de conexión |
| Operación | `vms/ops/` | Salud de imagen, reloj de cámaras, «¿Por qué no conecta?» (`diagnose.py`) |
| Analítica | `analytics/` | Conteo de personas y colas (opcional) |
| Panel central | `central/` | Estado de varias tiendas (opcional) |
| Actualizador TUF | `updater/` | Actualizaciones firmadas con vuelta atrás; **sin fuente configurada** en las instalaciones de GitHub |
| Nativos Rust | `native/` (`vmshost`, `vmsctl`, `common`) | Servicios de Windows, firewall, ACL, `vmsctl run/health/services` |
| Visor de escritorio | `native/viewer/` (Tauri 2.12.1 + WebView2, `VMS.exe`) | Ventana, bandeja, muros por monitor y **actualizar desde GitHub** (`src-tauri/src/github_update.rs`) |
| Instalador | `distribution/installer/VMSMultimarca.iss` (Inno Setup 7.1.0) y `tools/build/` | Un solo `.exe` con todo; el tipo de puesto decide qué servicios arrancan |

Más detalle en estos documentos:
- `LEEME.md`
- `docs/CONTRATO.md`, el contrato de interfaces: si cambias una API, actualízalo.
- `docs/PLAN-V2.md`
- `docs/EMPAQUETADO.md`
- `docs/PUBLICAR-VERSION.md`

## Preparar este PC (una vez)

```powershell
git clone -b v2 https://github.com/mauricioSas/vms-multimarca.git C:\src\vms-multimarca
cd C:\src\vms-multimarca
powershell -ExecutionPolicy Bypass -File tools\windows\preparar-entorno.ps1            # añade -Compilar para los .exe
```

El script hace lo siguiente:
- Instala con winget lo que falte: Git, Python 3.12, rustup, Node LTS, VS Build Tools C++ y FFmpeg.
- Instala Rust 1.99.0.
- Crea `.venv` con los locks del proyecto.
- Descarga MediaMTX y Chromium de Playwright.
- Pasa unas pruebas rápidas para comprobar que todo funciona.

Se puede repetir sin miedo. Si dice que falta algo después de instalarlo, cierra la ventana y abre otra: Windows
tiene que releer el PATH.

**Ojo: este PC tiene el producto instalado de verdad** (servicios `VMS*` en marcha y datos en
`C:\ProgramData\VMSMultimarca`). El modo desarrollo usa `.tmp\dev\` y el puerto 8600, el mismo que el producto
instalado. Antes de `tools.dev_run`, para los servicios instalados (como administrador):
`Get-Service VMS* | Stop-Service`. Después, para volver a dejarlos como estaban: `Get-Service VMS* | Start-Service`.
**Nunca ejecutes el e2e del instalador** (`tests.windows.run_e2e`) en este PC: instala y desinstala de verdad.
Ese e2e es solo para GitHub Actions o una máquina virtual.

## Órdenes del día a día

```powershell
.venv\Scripts\python -m pytest -m "not e2e and not slow" -q          # todas las pruebas (varios minutos)
.venv\Scripts\python -m pytest tests\ops\test_diagnose.py -q          # una sola parte
.venv\Scripts\python -m ruff check .                                  # lint (obligatorio)
.venv\Scripts\python -m mypy vms\core vms\ops                         # tipos (obligatorio)
cd native; cargo test --workspace --locked; cargo clippy --workspace --all-targets -- -D warnings; cargo fmt --check
.venv\Scripts\python -m tools.dev_run start --sim --seed --no-analytics   # app con cámaras simuladas → http://127.0.0.1:8600
.venv\Scripts\python -m tools.dev_run stop
.venv\Scripts\python -m tools.build all --version 2.0.0-dev --out dist    # instalador local (docs\EMPAQUETADO.md)
```

Hay dos pruebas que pueden fallar en máquinas lentas o en contenedores; en GitHub pasan:
- `tests/ops/test_health_imaging.py::test_under_20_ms_per_check_at_640_px`, que mide velocidad;
- en Linux, `run::tests::stubborn_child…` de `vmsctl`.

## Ramas, CI y cómo publicar una versión

- Se trabaja en **`v2`**. `integracion` va siempre igual que `v2`: se avanza la una con la otra, sin merges raros.
  `main` es la v1 antigua.
- El usuario autorizó a subir a `v2` y `integracion` directamente, y también a publicar versiones.
- **CI** está en `.github/workflows/ci.yml` y se lanza solo al subir a `v2`:
  - un job por bloque;
  - el instalador real se prueba en Windows con `windows-installer.yml` (e2e en `tests/windows/e2e/`);
  - para lanzarlo todo a mano: Actions → CI → Run workflow → `all`.
  - El e2e exige que el motor de vídeo responda tras instalar: es `_assert_engine_answers` en
    `test_02_silent.py`, y si falla vuelca `engine.log`.
- **Publicar una versión** (`publicar.yml`):
  1. Crea `docs/versiones/vX.Y.Z-beta.N.md` con las notas, en español sencillo. Copia el formato de las anteriores
     o de `PLANTILLA.md`.
  2. Escribe esa etiqueta en `docs/versiones/ACTUAL`.
  3. Haz commit y súbelo a `v2`.

  GitHub compila el instalador real y crea la *release* con el `.exe`, `SHA256SUMS.txt` y el SBOM. Tarda unos
  20 minutos. Las betas salen como *pre-release*. Comprueba que el `.exe` aparece en la release.
- El visor de los PC instalados busca versiones nuevas en GitHub Releases: a los 90 s de arrancar y luego cada 6 h.
  Las ofrece en la bandeja («Versión nueva X: descargar e instalar…») y en su página Actualizaciones.
  - Una beta ve betas; una final ve solo finales.
  - Verifica el SHA-256 e instala en silencio con el mismo tipo de puesto.
- Los mensajes de commit van en español y explican el porqué. No pongas nombres de modelos de IA en commits ni en
  código.

## Convenciones

- Todo el texto de cara al usuario va **en español de España neutro, sin voseo**. Hay un detector en las pruebas.
- Los errores se dicen con qué hacer: la regla es que haya un «Qué hacer:» en cada aviso.
- Seguridad:
  - nunca contraseñas en los registros (hay `redact`);
  - la búsqueda en red se limita a la LAN y a 5 paquetes/s;
  - las reglas de firewall nunca se abren en el perfil Público.
- Dependencias de Python: locks con SHA-256 (`tools/lock_requirements.py`). No añadas paquetes sin regenerar los
  locks.
- Cada arreglo lleva su prueba. Mejor si la prueba reproduce el fallo real y comprobaste que falla sin el arreglo.

## Estado a 7 de octubre de 2026

Versiones publicadas: `v2.0.0-beta.1` a `v2.0.0-beta.4`.
- **beta.3:** búsqueda por todas las tarjetas de red, firewall UDP 37020 para SADP y aviso de «cámara en otra red».
- **beta.4:** «¿Por qué no conecta?» ya no da error interno con cámaras sin hora (01-01-1970). En Windows,
  `astimezone()` falla antes de 1970.

**Cambios en `v2` sin publicar todavía:**
- La sección «Actualizaciones» de Estado del sistema muestra la versión nueva de GitHub
  (`vms/api/github_releases.py`). Antes decía en rojo «No hay fuente de actualizaciones configurada».
- El e2e exige que el motor de vídeo responda.

Publícalos en la próxima beta junto con el arreglo del motor.

### El PC del usuario (prueba real con una cámara)

- Windows 11, sin router: la cámara y el PC van por un switch. El PC tiene IP fija **192.168.1.80/24**, puerta
  192.168.1.1. Usa el WiFi para Internet.
- La cámara es una **Hikvision DS-2CD1741FWD-IZ** (firmware V5.5.0) y tiene **la fecha en 1970**.
  - Su IP de fábrica era 192.168.254.27. Se le dijo que la cambiara con SADP a 192.168.1.64, que es la que tiene
    dada de alta en el programa.
  - Usuario `admin`. En iVMS-4200 se ve bien.
- El usuario tiene **iVMS-4200 de Hikvision** instalado en el mismo PC. Puede que ocupe puertos.
- En nuestro programa:
  - **«¿Por qué no conecta?»**: red, puerto 80 y puerto 554 bien. La contraseña primero falló y luego la
    corrigió. Con la contraseña buena salía «Error interno», que es lo que arregla la beta.4.
  - **Estado del sistema → Motor de vídeo: «Detenido»**: «El motor de vídeo (servicio VMSEngine) no responde».
    Tenía la beta.2 instalada y aún no había actualizado.

### Lo primero que hay que hacer (pendiente)

1. **Averiguar por qué no arranca el motor de vídeo (VMSEngine) en su PC.** En GitHub, con una instalación
   limpia, sí arranca (CI del 7/10 verde con la comprobación nueva). Hipótesis por orden:
   - (a) choque de puertos con iVMS-4200 u otro programa. MediaMTX usa 127.0.0.1:8554 (RTSP), :9997 (API),
     :8889 (WebRTC) y :8189 (ICE). Mira `Get-NetTCPConnection -State Listen` y `Get-NetUDPEndpoint`.
   - (b) `mediamtx.yml` inválido después de dar de alta la cámara, por ejemplo por caracteres raros en la
     contraseña o en la ruta. Lo genera `vms/engine/service.py` → `mtx_config.py`.
   - (c) permisos de `NT SERVICE\VMSEngine` sobre `C:\ProgramData\VMSMultimarca\mediamtx` o sobre la carpeta de
     grabaciones.

   Para mirarlo, en PowerShell como administrador:

   ```powershell
   Get-Service VMS*
   Get-Content C:\ProgramData\VMSMultimarca\logs\engine.log -Tail 50
   $ctl = Get-ChildItem "C:\Program Files\VMSMultimarca\versions\*\bin\vmsctl.exe" | Select-Object -Last 1
   & $ctl health wait --timeout 10 --deep --json
   ```

   El registro del motor lo escribe `vmsctl run` (`native/vmsctl/src/run.rs`): `Kind::Engine` espera a que exista
   `mediamtx.yml` y lanza `engine\mediamtx.exe`. Arregla la causa, añade la prueba y publica la beta.
2. **Mostrar el error del motor en la propia app.** En Estado del sistema, añadir las últimas líneas de
   `engine.log` o el error de MediaMTX, para que el usuario no tenga que abrir PowerShell. El backend corre como
   `NT SERVICE\VMSBackend`, así que revisa las ACL de `logs\`. `vmsctl` puede dejar un resumen legible en
   `run-status`.
3. Cuando el motor funcione, comprobar con el usuario que la cámara se ve en el muro y graba. Después, ayudarle a
   poner la **hora de la cámara**: NTP apuntando al PC, o la hora del PC por ONVIF/ISAPI. Sin eso, las
   grabaciones salen con fecha de 1970.
4. Comprobar que **actualizar desde el visor** funciona de verdad en su PC: de beta.2 a la siguiente. Es la
   primera vez que se prueba en real.
5. «¿Por qué no conecta?» abierto desde la lista de equipos dijo «Este equipo aún no tiene cámaras dadas de
   alta» aunque tenía una. Revisa `vms/api/routes/diagnostics.py` y qué cuerpo manda la web (`device_id` o el
   formulario).

### Otras mejoras pendientes

- La búsqueda en red (beta.3) no se ha probado con una cámara real. Si la red de Windows es «Pública»
  (red no identificada, típico con un switch sin router), el firewall no deja pasar las respuestas SADP.
  Valora avisarlo en la propia pantalla de búsqueda.
- El instalador no está firmado: Windows avisa con «Windows protegió tu PC». Firmarlo cuesta dinero; es decisión
  del usuario.
- Pedirle al usuario que ponga `v2` como rama por defecto del repositorio en GitHub (Settings → Branches), para
  que la portada muestre el README bueno.
