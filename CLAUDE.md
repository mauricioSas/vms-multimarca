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

**Ojo: el PC de la cámara del usuario tiene el producto instalado de verdad** (servicios `VMS*` en marcha y datos
en `C:\ProgramData\VMSMultimarca`). Desde el 19/10/2026 el usuario desarrolla también desde otro PC con Windows:
antes de nada mira `Get-Service VMS*` para saber en cuál estás. Si hay servicios `VMS*`, sigue estas
precauciones. El modo desarrollo usa `.tmp\dev\` y el puerto 8600, el mismo que el producto instalado. Antes de `tools.dev_run`, para los servicios instalados (como administrador):
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

## Estado a 19 de octubre de 2026 (traspaso a otro PC)

Versiones publicadas en GitHub: `v2.0.0-beta.1` a `v2.0.0-beta.4`. **En el PC de la cámara está instalada una
`2.0.0-beta.5` compilada en local** (el 8/10, desde el código de `v2` de ese día menos los últimos arreglos). No
existe en GitHub: la siguiente que se publique debe ser **`v2.0.0-beta.6`** o mayor.

### Qué se hizo los días 8 a 10 de octubre en el PC de la cámara

Diagnóstico con los registros reales del producto instalado (`vmsctl diag bundle`) y arreglos, cada uno con su
prueba y revisado. Todo está en `v2` (este traspaso) salvo lo marcado como pendiente.

- **Causa del «Motor de vídeo: Detenido»:** el puerto 127.0.0.1:9998. Era el de las métricas de MediaMTX y
  también el WebSocket del servicio `SYS` de **HikCentral Professional** (instalado en ese PC y que el usuario
  usa: no se toca). Quien arrancaba primero se lo quedaba. Arreglo: métricas y pprof apagados siempre. Comprobado
  en real con la beta.5: el motor arranca y ya no abre el 9998.
- **Estado del sistema** dice por qué no arranca el motor (puerto y programa que lo ocupa, configuración mala…),
  avisa de la suspensión del portátil (batería, tapa, tiempo sin usar) y «Grabando» solo sale si se guarda vídeo.
- **Paradas ordenadas** del backend y del actualizador (antes se cerraban a la fuerza a los 10 s).
- **Reproducción**: dice por qué no hay vídeo; lo grabado de cámaras borradas también caduca por retención.
- **«¿Por qué no conecta?»**: equipo sin su vídeo dado de alta = fallo con «Qué hacer:»; paso nuevo con la
  tarjeta de red por la que sale el PC.
- **«Buscar en la red»**: explica la red «Pública» de Windows y el UDP 37020 ocupado (SADPServer de Hikvision).
- **Hora de las cámaras**: botón «Poner en hora con este PC» (Estado del sistema → Hora de los equipos) que
  ofrece también la **zona horaria** del PC (la cámara del usuario tiene la de fábrica, CST-8 de China).
- **Vídeo RTSP**: la cámara del usuario (Hikvision DS-2CD1741FWD-IZ, firmware V5.5.0) rechazaba la respuesta
  Digest con `algorithm=MD5` de más; «Probar conexión» decía que la contraseña era incorrecta y no lo era.
- **Visor de escritorio**: los muros daban «El servicio no acepta la clave de los muros» (401) porque el visor no
  entendía `kiosk.token` en formato `vms-dpapi-v1:`; «Abrir muro» del panel no hacía nada dentro del visor; el
  visor se creía la versión 2.0.0-dev.0 y pedía «reiniciar el visor» sin motivo; `/api/health` decía 0.1.0.
- **Monitores**: una cámara borrada y vuelta a añadir recupera su celda; una nueva va al primer hueco del monitor 1.
- **Pruebas en Windows**: nunca leen ni tocan la instalación real ni sus puertos; PostgreSQL de pruebas con zonas
  horarias. `tools/windows/preparar-entorno.ps1` arreglado (se cortaba con otra versión de Python instalada).

Lo que entró en la beta.5 instalada y lo que no: la beta.5 tiene todo lo anterior **excepto** la zona horaria
del botón de hora, el arreglo de RTSP, el del visor (muros) y el de Monitores. Esos llegan con la beta.6.

### El PC de la cámara (estado real el 8/10)

- Windows 11 portátil (Modern Standby: se suspende con la tapa o a batería y entonces no graba). PC con IP fija
  **192.168.1.80/24** en el cable; la cámara y el PC van por un switch PoE sin router. El WiFi de casa está en
  la misma red 192.168.1.x (con el cable a 1 Gbps Windows prefiere el cable; con el cable quitado, el WiFi).
- Cámara **Hikvision DS-2CD1741FWD-IZ** en **192.168.1.64**, usuario `admin` (la contraseña la sabe el usuario;
  nunca la escribas en el repositorio). Fecha en 1970 y zona CST-8. **La imagen sale boca abajo** (montada en
  el techo); el usuario pidió no girarla todavía (ISAPI `ImageFlip` o la web de la cámara).
- Tras muchos intentos fallidos la cámara **bloquea unos 30 minutos a todo el PC** (también la contraseña buena).
  Si el programa dice «Usuario o contraseña incorrectos» varias veces seguidas, espera antes de repetir.
- El usuario borró y volvió a añadir el equipo varias veces: ahora es «Camara» (`dev-488091e1`, cámara
  `cam-fc49d3e6`), en el monitor 1. Graba y se ve en Reproducción. El directo funciona en el navegador
  (`http://127.0.0.1:8600` → Monitores → Abrir muro); en el visor de escritorio no hasta instalar la beta.6.
- HikCentral: `SYS.exe` (9999, 10001…), nginx en 0.0.0.0:80, `stream.exe` en 0.0.0.0:554, `SADPServer.exe` en UDP
  37020, PostgreSQL 5432. Corre como LocalSystem: abrir un puerto suyo da 10013, no 10048.
- `kiosk.token` está protegido (`vms-dpapi-v1:`); el usuario de Windows está en «VMS Operadores».
- Herramientas de ese PC: Inno Setup 7.1.0 sin administrador en `C:\src\tools\inno7` (`$env:VMS_ISCC`).

### Lo siguiente (en orden)

1. **Publicar la beta.6** con todo lo de arriba: `docs/versiones/v2.0.0-beta.6.md` en español sencillo
   (incluye los cambios de comportamiento: retención de lo grabado de cámaras borradas, `/api/health` con la
   versión real, la suspensión del portátil) y `docs/versiones/ACTUAL`. Antes, comprobar que la CI de `v2` está en
   verde. Ojo: la beta.5 local tiene el visor antiguo, que comparaba mal las betas: puede que **no ofrezca** la
   beta.6. Si no la ofrece, el usuario la instala descargando el `.exe` de la release y ejecutándolo (pide
   administrador) o con esta línea en PowerShell de administrador, cambiando la ruta:
   `Start-Process <ruta>\VMSMultimarca-Setup-2.0.0-beta.6.exe -ArgumentList '/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS','/TYPE=control' -Wait`.
2. **En el PC de la cámara, con el usuario delante**, tras instalar:
   - Cerrar el visor (bandeja → «Salir (la grabación sigue)») y volver a abrirlo; abrir el Muro 1 desde la
     bandeja y desde «Abrir muro»: debe verse en directo.
   - «Probar conexión» de la cámara: debe dar el vídeo RTSP correcto (una sola vez).
   - Estado del sistema → Hora de los equipos → «Poner en hora con este PC» con la zona de Madrid. Pedirá el
     usuario administrador de la cámara si el guardado no vale.
   - Comprobar «Grabando», la sección «Energía del PC» y que «Diagnóstico» del visor pone la clave de los muros
     como «Lista para usar».
3. Si el usuario quiere: girar la imagen 180°; decidir qué hacer con la suspensión del portátil.
4. Comprobar que **actualizar desde el visor** funciona de verdad (de la beta.6 a la siguiente).

### Lecciones de estas sesiones (para no repetir errores)

- Para mirar el producto instalado sin ser administrador: `vmsctl version show`, `services status` y `health wait`
  funcionan; los registros no. El usuario puede ejecutar un script de solo lectura que use `vmsctl diag bundle`
  (tapa secretos y no incluye `mediamtx.yml`) y deje el zip en una carpeta que puedas leer.
- `tools.build all` necesita el visor ya compilado (`npx --yes @tauri-apps/cli@2.12.1 build --no-bundle --ci` en
  `native\viewer\src-tauri`) y no admite `CARGO_TARGET_DIR` cambiado (busca `native\target\release`).
- Con varios agentes en worktrees: **no uses `git stash`** (es común a todos los worktrees y se mezclan
  cambios); usa worktrees temporales. A un agente de un workflow en marcha no se le puede escribir: el mensaje
  arranca un gemelo que trabaja en la misma carpeta.
- PowerShell 5.1 quita las comillas dobles de los argumentos de `python -c`: escribe el script en un archivo.
- Una prueba de `tests/ops/test_clock_forecast.py` llenaba el disco en Windows (113 GB): está arreglada; si una
  prueba se cuelga, mira el espacio libre.

### Otras mejoras pendientes

- La búsqueda en red no se ha probado con una cámara real: comprobar que `NT SERVICE\VMSBackend` puede leer el
  perfil de red y que SADP convive con HikCentral.
- En Windows, el backend usa psycopg **asíncrono** en `vms/api/routes/counts.py` y en `vms/ops/service.py`
  (`_analytics_alerts`); con Proactor, con `VMS_PG_DSN` el CSV responde 503. Lo reproduce
  `tests/ops/test_central_and_counts.py::test_store_counts_csv_needs_pg_and_uses_site_timezone`. Arreglo:
  psycopg síncrono en `asyncio.to_thread`.
- `migrate-from-v1` sin `--role` adivinó «tienda» y dejó en las ACL dos SID de servicios ya borrados
  (VMSAnalytics, VMSHeartbeat): solo limpieza, prioridad baja.
- El instalador no está firmado: Windows avisa con «Windows protegió tu PC». Firmarlo cuesta dinero; es decisión
  del usuario.
- Pedirle al usuario que ponga `v2` como rama por defecto del repositorio en GitHub (Settings → Branches), para
  que la portada muestre el README bueno.
