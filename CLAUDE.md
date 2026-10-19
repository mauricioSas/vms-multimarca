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

En Windows, y sobre todo en un PC con el producto instalado (como este):
- **Nunca ejecutes `cargo test` ni `pytest` desde una consola de administrador.** Abre PowerShell normal. Las
  pruebas están aisladas, pero si un día falla el aislamiento, sin administrador Windows responde «Acceso
  denegado» y la prueba falla; como administrador, cambiaría la instalación real (grupo «VMS Operadores», clave
  `HKLM\SOFTWARE\VMSMultimarca`, `C:\ProgramData\VMSMultimarca`).
- Qué aísla cada cosa:
  - `pytest` solo lee su propio `.env`: `tests/conftest.py` fija `VMS_ENV_FILE_ONLY=1` (CONTRATO §3.1), así que
    no lee ni el `.env` del repositorio ni `C:\ProgramData\VMSMultimarca\.env`.
  - Un vigilante del `conftest` hace fallar la prueba que abra, liste, cree o borre algo en
    `C:\ProgramData\VMSMultimarca`, `C:\Program Files\VMSMultimarca` o las carpetas que anota el registro
    (`HKLM\SOFTWARE\VMSMultimarca`), o que lance un programa cuya línea de órdenes las nombre. Vigila solo el
    proceso de pytest: lo que hagan por dentro los programas que lanza, el registro y `ctypes` no los ve. Solo se
    salta en el e2e del instalador.
  - Otro vigilante (`ProductPortGuard`) hace fallar la prueba que se conecte en este equipo a los puertos por
    defecto del programa (8600, 8643, 8554, 8889, 8189, 9996 y 9997), donde responde el programa instalado. Usa
    la fixture `settings`, `FakeEngine` o `get_free_port()`; para hablar de verdad con el programa instalado,
    `@pytest.mark.product_ports`. Tampoco ve los procesos hijos ni Chromium: dales puertos libres.
  - `cargo test` de `vmsctl` crea el grupo y escribe el registro a través de un doble en memoria (`sys::Runner`).
  - `tools.dev_run` también fija `VMS_ENV_FILE_ONLY=1` en sus procesos y no arranca si `--env-file` no tiene
    `VMS_DATA_DIR`.
- El PostgreSQL de pruebas (`pgserver`) se prepara solo con zonas horarias en `.tmp\pginstall-*`
  (`tools/pgserver_tz.py`) y corre en UTC. Las pruebas asíncronas de `central/`, `analytics/` y `db/` usan
  `SelectorEventLoop`, como esos procesos; las del backend, Proactor, como el backend.

Hay dos pruebas que pueden fallar en máquinas lentas o en contenedores; en GitHub pasan:
- `tests/ops/test_health_imaging.py::test_under_20_ms_per_check_at_640_px`, que mide velocidad;
- en Linux, `run::tests::stubborn_child…` de `vmsctl`.

En este PC con Windows (8/10/2026), sin e2e ni slow, pasan unas 1850 pruebas y fallan unas 13. Antes fallaban
unas 60: la mayoría eran de PostgreSQL (zonas horarias y bucle Proactor), ya arregladas. La CI no pasa pytest en
Windows, así que la referencia es GitHub (verde). Las que siguen fallando aquí fallan igual sin tus cambios:
- no hay `bash`: 3 de `tests/central/test_deploy.py`;
- cosas de Windows: DPAPI en `test_review_credentials.py`, la tubería con nombre del actualizador y
  `test_release_tools.py::test_keyring_rules`;
- tiempo, red o navegador: `test_health_imaging`, `test_rtsp_chaos`, `test_hikvision_dahua` y `test_evidence`
  (`test_ui_b6` y `test_wall_sessions` fallaban porque les contestaba el programa instalado; ya pasan);
- 3 de `tests/web/test_ui.py` (`test_add_device_and_assign_to_monitor`, `test_vendor_selector_fills_paths` y
  `test_pages_fit_phone_width[/]`): «No se pudo leer el registro de marcas» y un 404 en la consola. Fallan al
  pasar ese archivo solo (también en `91b123f`); dentro de la batería completa de `vms/` pasan;
- `test_store_counts_csv_needs_pg_and_uses_site_timezone`, que es un fallo real del backend (ver «Otras mejoras
  pendientes»).

Para trabajar en `vms/`, pasa las pruebas de esa parte (por ejemplo `tests\ops tests\api tests\vendors`). Si
dudas de si un fallo es tuyo, pasa esa misma prueba en `v2` sin tu cambio.

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

## Estado a 8 de octubre de 2026

Versiones publicadas: `v2.0.0-beta.1` a `v2.0.0-beta.4`.
- **beta.3:** búsqueda por todas las tarjetas de red, firewall UDP 37020 para SADP y aviso de «cámara en otra red».
- **beta.4:** «¿Por qué no conecta?» ya no da error interno con cámaras sin hora (01-01-1970). En Windows,
  `astimezone()` falla antes de 1970.

**Cambios en `v2` sin publicar todavía** (para las notas de la próxima beta; aún no hay archivo en
`docs/versiones/`):
- La sección «Actualizaciones» de Estado del sistema muestra la versión nueva de GitHub
  (`vms/api/github_releases.py`). Antes decía en rojo «No hay fuente de actualizaciones configurada».
- El e2e exige que el motor de vídeo responda.
- **Motor de vídeo:** arranca aunque HikCentral use el puerto 9998 (métricas y pprof de MediaMTX apagados
  siempre), y si no arranca por otro motivo, Estado del sistema dice por qué y qué hacer.
- **Paradas:** al apagar, reiniciar o actualizar, los servicios paran solos y en orden en uno o dos segundos; antes
  `vmsctl` los cerraba a la fuerza a los 10 s. El plazo nuevo de VMSBackend (12 s) solo vale con el `vmsctl` nuevo.
- **Reproducción:** dice por qué no hay vídeo y «Grabando» solo sale si de verdad se guarda. **Cambio que hay que
  contar:** lo grabado de cámaras borradas, desactivadas o en pausa por contraseña ya no se queda para siempre: se
  borra al cumplir los días de retención (RGPD; CONTRATO §4.3).
- **Energía:** Estado del sistema avisa si Windows va a suspender el PC (batería, tiempo sin usar, tapa) y dejar de
  grabar.
- **Diagnóstico:** «¿Por qué no conecta?» marca como fallo un equipo guardado sin su vídeo (qué hacer: «Canales»)
  y dice por qué tarjeta de red sale el PC hacia la cámara.
- **Búsqueda:** «Buscar en la red» explica por qué no encuentra cámaras: red «Pública» de Windows (con la línea
  de PowerShell para copiar) o el puerto UDP 37020 de SADP ocupado.
- **Hora de las cámaras:** botón «Poner en hora con este PC» (solo administradores, una sola escritura, sin
  cambiar la zona) y aviso en segundos si la cámara vuelve a 1970 tras un corte. En «¿Por qué no conecta?» la hora
  puesta a mano es un aviso, no la causa.
- **Pruebas en Windows:** las pruebas nunca leen ni tocan la instalación real y el PostgreSQL de pruebas tiene
  zonas horarias. No cambia el programa instalado.

Publícalos en la próxima beta.

### El PC del usuario (prueba real con una cámara)

- Windows 11, sin router: la cámara y el PC van por un switch. El PC tiene IP fija **192.168.1.80/24**, puerta
  192.168.1.1. Usa el WiFi para Internet, y el WiFi también tiene una IP de 192.168.1.x: con el cable de red
  desconectado, Windows busca la cámara por el WiFi (lo dice el paso «Tarjeta de red de este PC» del diagnóstico).
- La cámara es una **Hikvision DS-2CD1741FWD-IZ** (firmware V5.5.0) y tiene **la fecha en 1970**.
  - Su IP de fábrica era 192.168.254.27. Se le dijo que la cambiara con SADP a 192.168.1.64, que es la que tiene
    dada de alta en el programa.
  - Usuario `admin`. En iVMS-4200 se ve bien.
- En el mismo PC están **HikCentral Professional** (servicios de Windows que arrancan solos, como LocalSystem) e
  **iVMS-4200** de Hikvision. Puertos de HikCentral que nos afectan:
  - `SYS.exe`: 127.0.0.1:**9998** (WebSocket) y 9999. El 9998 era el de las métricas de MediaMTX y por eso el
    motor no arrancaba (pendiente 1). El 8/10 HikCentral movió su WebSocket al 10001 por su cuenta y lo guardó.
  - nginx en 0.0.0.0:80 y `stream.exe` en 0.0.0.0:554 (son de HikCentral, no de iVMS-4200).
  - `SADPServer.exe`: **UDP 37020**, el puerto de nuestra búsqueda SADP.
  - Como HikCentral corre con otra cuenta, abrir un puerto que él tiene en 0.0.0.0 da el error 10013, no el
    10048. Aquí un 10013 no es un rango reservado de Hyper-V, aunque `vmsctl ports check` lo diga así.
- En nuestro programa (tenía la beta.2 instalada y aún no había actualizado):
  - **«¿Por qué no conecta?»**: red, puerto 80 y puerto 554 bien. La contraseña primero falló y luego la
    corrigió. Con la contraseña buena salía «Error interno», que es lo que arregla la beta.4.
  - La cámara se guardó con la contraseña mala, así que **no se importó su vídeo** (pendiente 5).
  - **Estado del sistema → Motor de vídeo: «Detenido»**: «El motor de vídeo (servicio VMSEngine) no responde».
    La causa era el puerto 9998 (pendiente 1).

### Lo primero que hay que hacer (pendiente)

1. **Motor de vídeo «Detenido»: la causa está encontrada y arreglada en `v2`; falta comprobarlo tras instalar.**
   - Causa: el `.env` de la v1 (copiado de `.env.example`) fijaba `VMS_MTX_METRICS_ADDRESS=127.0.0.1:9998`, el
     WebSocket de HikCentral (servicio SYS). MediaMTX abre primero el puerto de métricas: si HikCentral lo tenía,
     se cerraba al arrancar. `engine.log` tenía 1306 caídas seguidas con «listen tcp 127.0.0.1:9998: bind» (6 y
     7/10, beta.1 y beta.2). El 8/10 el motor arrancó antes que HikCentral y funcionó; desde entonces HikCentral
     usa el 10001.
   - Arreglo (commit «Motor de vídeo: arranca aunque HikCentral use el 9998…»): métricas y pprof de MediaMTX
     apagados siempre (`vms/engine/mtx_config.py`). Desaparece `VMS_MTX_METRICS_ADDRESS`; si el `.env` instalado
     la trae, se ignora, también en `vmsctl ports check`. CONTRATO §4.1 y §4.3.
   - Pendiente: tras instalar la próxima beta, comprobar en su PC que Estado del sistema → Motor de vídeo pone «En
     marcha» y que `C:\ProgramData\VMSMultimarca\mediamtx\mediamtx.yml` tiene `metrics: false` y ningún 9998 (el
     backend lo reescribe al arrancar). Si el motor vuelve a no arrancar, el motivo ya sale en Estado del sistema
     (punto 2). Para mirarlo a mano, en PowerShell como administrador:

     ```powershell
     Get-Service VMS*
     Get-Content C:\ProgramData\VMSMultimarca\logs\engine.log -Tail 50
     $ctl = Get-ChildItem "C:\Program Files\VMSMultimarca\versions\*\bin\vmsctl.exe" | Select-Object -Last 1
     & $ctl health wait --timeout 10 --deep --json
     ```
2. **Hecho: el error del motor se ve en la propia app.** `vms/engine/problem.py` interpreta `engine.log` y
   `logs\status-VMSEngine.json`, y `EngineStatus.problem` (`{code, detail_es, action_es}`, CONTRATO §6.8 y
   §13.10) sale como aviso en Estado del sistema con su «Qué hacer:». Si un puerto está ocupado, nombra el puerto
   y, si se sabe con certeza, el programa. Vale aunque solo se actualice la app, sin un `vmsctl` nuevo. Falta
   comprobar en su PC que `NT SERVICE\VMSBackend` puede leer `status-VMSEngine.json`; si no, `vms.log` lo dice
   una vez y el aviso sale igual a partir de `engine.log`.
3. Cuando el motor funcione y la cámara tenga su vídeo dado de alta (punto 5), comprobar con el usuario que se ve
   en el muro y graba («Grabando» en Estado del sistema en un minuto). Después, poner la **hora de la cámara** con
   el botón «Poner en hora con este PC» (Estado del sistema → Hora de los equipos; CONTRATO §18.3). El botón llega
   con la próxima beta; la beta.2 instalada tampoco tiene el arreglo de la beta.4 para relojes de 1970. Las
   grabaciones llevan siempre la hora de este PC: con la cámara en 1970 solo sale mal la fecha que ella escribe en
   la imagen. Este PC no sirve la hora por NTP, así que no sirve «NTP apuntando al PC». El botón no cambia la zona
   horaria de la cámara (la de fábrica es UTC+8): hay que ponerla en su web. Tras pulsarlo, la cámara queda con la
   hora puesta a mano y «¿Por qué no conecta?» lo da como aviso, no como fallo. Falta probarlo con su cámara
   (firmware V5.5.0).
4. Comprobar que **actualizar desde el visor** funciona de verdad en su PC: de beta.2 a la siguiente. Es la
   primera vez que se prueba en real.
5. **La cámara no tiene su vídeo dado de alta.** «¿Por qué no conecta?» decía «Este equipo aún no tiene cámaras
   dadas de alta» y era verdad: se guardó con la contraseña mala, así que no se importaron sus canales, y corregir
   la contraseña en «Editar» no los importa (`vms.log` del 6/10). **Lo que tiene que hacer el usuario** (ya con
   la beta.2): en «Equipos y monitores», pulsar «Canales» en la fila de la cámara y marcarla. Si la contraseña
   guardada aún falla, que la corrija antes en «Editar». Desde la próxima beta, «¿Por qué no conecta?» lo da como
   fallo con ese «Qué hacer:», y al guardar un equipo sin poder importar sus canales sale el mismo aviso.

### Otras mejoras pendientes

- La búsqueda en red (beta.3) no se ha probado con una cámara real. Si no encuentra nada, ya explica por qué
  encima de la lista (`report` de `POST /api/discovery/scan`, CONTRATO §6.3): red de Windows «Pública», con
  la línea `Set-NetConnectionProfile` para copiar, o el UDP 37020 ocupado por `SADPServer.exe`. Falta
  comprobar en el PC del usuario que la cuenta `NT SERVICE\VMSBackend` puede leer el perfil de red (si no,
  no sale el aviso de «Pública») y que SADP convive con HikCentral (`vms.log`: «se escucha por cada tarjeta»).
- En Windows, el backend usa psycopg **asíncrono** en `vms/api/routes/counts.py` (CSV de conteos) y en
  `vms/ops/service.py` (`_analytics_alerts`, avisos de la analítica en la línea de tiempo). El backend corre
  con Proactor, así que con `VMS_PG_DSN` el CSV responde 503 y la línea de tiempo pierde esos avisos. Lo
  reproduce `tests/ops/test_central_and_counts.py::test_store_counts_csv_needs_pg_and_uses_site_timezone`.
  Arreglo: psycopg síncrono en `asyncio.to_thread`, como el latido.
- El instalador no está firmado: Windows avisa con «Windows protegió tu PC». Firmarlo cuesta dinero; es decisión
  del usuario.
- Pedirle al usuario que ponga `v2` como rama por defecto del repositorio en GitHub (Settings → Branches), para
  que la portada muestre el README bueno.
