# CONTRATO TÉCNICO — VMS Multimarca

Fuente de verdad de interfaces entre módulos. Versión **2.0** · 5 de octubre de 2026 (fase 0 de la v2;
la 1.0 es del 4 de octubre). Las secciones §1-§12 describen la v1 y siguen vigentes salvo donde una
sección nueva diga lo contrario; **§13-§18 son la v2** (plan: [`docs/PLAN-V2.md`](PLAN-V2.md)).
Si necesitas cambiar algo de aquí: hazlo de forma **mínima y compatible**, y anótalo en la
sección **§12 Cambios** (fecha, quién, qué y por qué). Nunca cambies en silencio un nombre,
un puerto, un campo JSON o una firma que otro módulo usa.

Plan aprobado: `PLAN.md` (v1) y `docs/PLAN-V2.md` (v2). Código de referencia de este contrato: `vms/core/` (modelos,
interfaces, ajustes) y `vms/db/migrations/` (esquema SQL). **Si el texto y el código no
coinciden, manda el código de `vms/core` y se corrige el texto.**

---

## 0. Reglas que aplican a todo el código

| Tema | Regla |
|---|---|
| Idioma | Identificadores en inglés. Textos de interfaz, mensajes de error al usuario, logs dirigidos al instalador, LEEME y docs en **español neutro con tuteo** (tú/tienes/quieres). **Nunca voseo** (`vos`/`tenés`/`querés`; lo comprueba `tests/test_spanish_style.py`). |
| Entregable | No mencionar en código, comentarios ni docs herramientas internas de desarrollo ni autoría por IA. El informe semanal usa un «proveedor LLM configurable» (eso sí se documenta). |
| Licencias | Solo MIT/BSD/Apache/PSF/ISC/MPL y LGPL con enlace dinámico. Prohibido: ver §10. Ante la duda, no se usa. `tests/test_licenses.py` lo vigila. |
| RGPD | La analítica procesa cada frame en memoria y lo descarta. **Nunca** se guardan imágenes ni vídeo de la analítica, ni identificadores de personas, ni reconocimiento facial, ni nada que mida a trabajadores. Solo conteos anónimos agregados. El snapshot para dibujar zonas se sirve en memoria y **no se escribe en disco**. |
| Secretos | Nunca en código ni en JSON plano. Ajustes de proceso en `.env` (`VmsSettings`, `SecretStr`). Contraseñas de equipos en `CredentialStore` (keyring o archivo cifrado). Logs con `vms.core.logging_setup` (ocultan credenciales en URLs, query strings, cabeceras y tokens de Telegram). `CameraSource` no imprime sus URLs. |
| Plataformas | Destino: **Windows 10/11** (PC de control, 4 monitores) y **Windows o Linux** (mini PC de tienda). Desarrollo y pruebas en macOS arm64. `pathlib` siempre; nada POSIX sin alternativa Windows (señales, permisos, rutas, `start_new_session` vs `CREATE_NEW_PROCESS_GROUP`). |
| Calidad | Tipado, errores de dominio (`vms.core.errors`), logs, reconexión automática con backoff, **ninguna excepción silenciada sin log**, pruebas pytest. Si algo no se pudo probar, se dice. |
| Git | Los bloques **no** hacen commit ni push. El arquitecto y el integrador trabajan en la rama `v2` del repositorio privado (commits pequeños, push solo a `v2`). |

---

## 1. Estructura y dueños

Cuatro agentes trabajan en paralelo. Cada uno **solo edita lo suyo**. Lo compartido es del
arquitecto; si un agente necesita un cambio imprescindible en lo compartido, puede hacer un
cambio **aditivo y mínimo** (nunca incompatible) y lo anota en §12.

```
vms-multimarca/
├── PLAN.md                    plan aprobado (no tocar)
├── LEEME.md                   guía rápida del repositorio                     [arquitecto]
├── pyproject.toml             paquetes y extras                               [arquitecto]
├── requirements-*.txt         locks por extra (tools/lock_requirements.py)    [arquitecto]
├── .env.example               variables de entorno documentadas               [arquitecto]
├── docs/CONTRATO.md           este documento                                  [arquitecto]
├── vms/
│   ├── __init__.py            APP_NAME, __version__                           [arquitecto]
│   ├── __main__.py            `python -m vms` → arranca el backend            [api-web]
│   ├── core/                  modelos, ajustes, credenciales, logs, rutas,    [arquitecto]
│   │                          interfaces (Protocol), errores, sources
│   ├── db/                    migraciones SQL + `python -m vms.db.migrate`    [arquitecto]
│   ├── vendors/               ISAPI Hikvision, CGI/RPC2 Dahua, ONVIF,         [vendors-engine]
│   │                          WS-Discovery propio, prueba de conexión
│   ├── engine/                mediamtx.yml, supervisor del proceso, cliente   [vendors-engine]
│   │                          de API/playback, disk guard
│   ├── api/                   FastAPI: REST, auth, proxy WHEP/playback, SSE   [api-web]
│   └── web/                   HTML/CSS/JS estático (panel, muro, reproducción,[api-web]
│                              dibujo de zonas). Sin paso de compilación.
├── analytics/                 detector, tracking, línea/zona, agregación,     [analytics]
│                              alertas Telegram, persistencia, informe semanal,
│                              exportación del modelo a ONNX/OpenVINO
├── central/                   panel central multi-sede + heartbeat (emisor    [central-deploy]
│                              y lector)
├── deploy/                    instalador Windows, servicio, kiosco 4          [central-deploy]
│                              monitores, systemd Linux
├── tools/                     simulador de cámaras, mocks de fabricantes,     [arquitecto]
│                              descarga de MediaMTX y vídeos, locks
├── tests/
│   ├── conftest.py, fakes.py  fixtures y dobles compartidos                   [arquitecto]
│   ├── core/ db/ tools/       pruebas de lo compartido                        [arquitecto]
│   ├── vendors/ engine/       [vendors-engine]
│   ├── api/ web/              [api-web]
│   ├── analytics/             [analytics]
│   ├── central/               [central-deploy]  (también pruebas de deploy/)
│   ├── e2e/test_infra_chain.py  [arquitecto]; resto de tests/e2e/ → [api-web]
│   └── assets/                vídeos de prueba (no versionados)
├── legacy/                    código anterior, NO se empaqueta ni se importa
└── bin/                       mediamtx descargado (no versionado)
```

Prefijos de logger: `vms.vendors.*`, `vms.engine.*`, `vms.api.*`, `analytics.*`, `central.*`.

**v2:** la estructura nueva (carpetas `native/`, `distribution/`, `updater/`, `infra/`, `vms/ops/`,
`spikes/`…) y los dueños de cada carpeta y de cada archivo compartido están en
[PLAN-V2 §6.2](PLAN-V2.md) y en el `LEEME.md` de cada carpeta. Prefijo de logger nuevo: `vms.ops.*` (B6).

---

## 2. Entorno de desarrollo

- Python **3.12**. Venv: `.venv/` (ya creado con todo instalado; **no instales nada global**:
  nada de brew, sudo ni Docker). Si un agente necesita un paquete nuevo: comprueba licencia,
  instálalo en el venv, añádelo al extra en `pyproject.toml`, regenera locks con
  `.venv/bin/python -m tools.lock_requirements` y anótalo en §12.
- Instalación reproducible: `pip install --no-deps -r requirements-dev.txt && pip install --no-deps -e .`
  (`--no-deps` es obligatorio: ver §10, PyAV).
- MediaMTX v1.21.1 en `bin/mediamtx` (`python -m tools.fetch_mediamtx [--platform windows_amd64]`,
  verifica SHA-256 contra la release oficial). El código lo localiza con `vms.core.paths.find_mediamtx()`
  (`VMS_MEDIAMTX_BIN` → `<instalación>/bin/` → PATH).
- ffmpeg (`/opt/homebrew/bin/ffmpeg`) **solo** para generar flujos de prueba. El producto no lo usa.
- Chromium de Playwright instalado **dentro del venv** (`PLAYWRIGHT_BROWSERS_PATH=0`, lo fija `conftest.py`).
- Pruebas: `.venv/bin/python -m pytest` (todo) · `-m "not e2e"` (rápidas) · `tests/<módulo>`.
  Marcadores: `slow`, `e2e`, `needs_ffmpeg`, `needs_mediamtx`, `needs_postgres`, `needs_browser`, `needs_model`.
- Temporales de pruebas en `.tmp/` (ignorado). Cualquier proceso que arranque una prueba se para al acabar.

---

## 3. Configuración

### 3.1 Ajustes de proceso: `.env` → `vms.core.settings.VmsSettings`

Prefijo `VMS_`. Orden: valores por defecto → `<instalación>/.env` → `<datos>/.env` →
`VMS_ENV_FILE` → variables reales. Valores vacíos = sin definir. Lista completa y comentada
en `.env.example`. Se cargan con `load_settings()`. Lo esencial:

| Variable | Defecto | Uso |
|---|---|---|
| `VMS_DATA_DIR` | por SO (§3.2) | carpeta de datos |
| `VMS_SITE_ID` | `site-local` | id de sede, único entre tiendas (`^[a-z0-9][a-z0-9-]{2,39}$`) |
| `VMS_HTTP_HOST` / `VMS_HTTP_PORT` | `0.0.0.0` / `8600` | backend |
| `VMS_ADMIN_INITIAL_PASSWORD` | — | crea el usuario `admin` en el primer arranque |
| `VMS_KIOSK_TOKEN`, `VMS_KIOSK_ALLOW_REMOTE` | —, `false` | acceso de los muros en kiosco |
| `VMS_INTERNAL_TOKEN` | autogenerado en `secrets/internal.token` | analítica ↔ backend (`settings.ensure_internal_token()`) |
| `VMS_CREDENTIAL_BACKEND`, `VMS_SECRET_KEY` | `auto`, — | almacén de contraseñas de equipos |
| `VMS_MEDIAMTX_BIN` | `<instalación>/bin/mediamtx` | binario |
| `VMS_MTX_*_ADDRESS` | ver §4.1 | direcciones de MediaMTX |
| `VMS_MTX_WEBRTC_ADDITIONAL_HOSTS` | `[]` | IPs extra para ICE (VPN) |
| `VMS_PG_DSN` | — | PostgreSQL (conteos, latidos, informes) |
| `VMS_HEARTBEAT_SECONDS` | `60` | latido de la sede |
| `VMS_TELEGRAM_BOT_TOKEN` | — | alertas |
| `VMS_LLM_PROVIDER`, `VMS_LLM_MODEL`, `VMS_LLM_API_KEY` | `anthropic`, —, — | informe semanal |

Una cadena vacía en un ajuste de dirección (p. ej. `mtx_webrtc_ice_tcp=""`) significa **desactivado**.
Ojo: en el `.env` una variable vacía significa «valor por defecto» (`env_ignore_empty`); para desactivar
ICE por UDP o por TCP desde el `.env` se escribe `off` (`VMS_MTX_WEBRTC_ICE_TCP=off`). Ver §12.

### 3.2 Carpeta de datos (`vms.core.paths.AppPaths`)

| SO | Ruta por defecto |
|---|---|
| Windows | `%PROGRAMDATA%\VMSMultimarca` (el instalador restringe `secrets\` con `icacls`) |
| Linux | `$XDG_DATA_HOME/vms-multimarca` o `~/.local/share/vms-multimarca`; el servicio systemd fija `VMS_DATA_DIR=/var/lib/vms-multimarca` |
| macOS (solo desarrollo) | `~/Library/Application Support/VMSMultimarca` |

Subcarpetas: `config/` (`config.json`, `config.json.bak`, `users.json`), `secrets/`
(`secret.key`, `credentials.enc`, `internal.token`; permisos solo propietario), `logs/`,
`recordings/` (por defecto), `mediamtx/` (`mediamtx.yml` generado), `analytics/`
(`status.json`, `config-cache.json`, `spool/`).

### 3.3 `config.json` = `vms.core.models.AppConfig`

> **v2 (§13.8):** `version` = 2 (migración automática desde la 1), los modelos persistentes conservan
> campos desconocidos y hay campos nuevos con valor por defecto (`allow_basic`, `follow_ip`, `identity`,
> `Camera.health`, `settings.notifications`, `settings.health`, `User.camera_scope`).

Un único documento JSON con guardado atómico, copia `.bak` y recuperación (`ConfigStore`).
En el backend **solo** se modifica con `ConfigRepository.update(fn)`, que serializa los
cambios, revalida el documento entero, guarda y notifica a los suscriptores (motor, SSE).
`ConfigRepository.revision` sube en cada cambio. Contenido:

- `devices[]: Device` — equipo físico (cámara IP o NVR). Sin contraseña.
- `cameras[]: Camera` — un canal de vídeo; `device_id` + `channel`. Rutas manuales opcionales
  (`main_path`, `sub_path`) que mandan sobre el preset; `has_sub=false` = sin subflujo.
- `walls[4]: WallLayout` — monitor 1..4, `grid` ∈ {1,4,9,16}, `cells` siempre 16 (id de cámara o null).
- `analytics_cameras[]: CameraAnalytics` — activación, fps (puerta 10-15, cajas 1-2), detector, confianza.
- `analytics_rules[]: LineRule | ZoneRule` — discriminadas por `kind` (`"line"`/`"zone"`).
- `settings: SystemSettings` — `site`, `retention` (`days`, `disk_guard_percent`),
  `recording` (`segment_seconds`, `part_seconds`, `recordings_dir`), `alerts` (`telegram_enabled`, `telegram_chat_id`).

Borrados en cascada: `AppConfig.remove_device()` / `remove_camera()` limpian celdas de muros,
reglas y ajustes de analítica. Además, al borrar un equipo, la API borra su contraseña.

### 3.4 Identificadores

`vms.core.naming.new_id(prefix)` → `dev-xxxxxxxx`, `cam-xxxxxxxx`, `rule-xxxxxxxx`
(8 hex). Patrón `^[a-z0-9][a-z0-9-]{2,39}$`. Son estables: se usan en rutas de MediaMTX,
carpetas de grabación y PostgreSQL. **Nunca** se reutiliza un id.

### 3.5 Usuarios (`users.json` = `UserStore`)

`User{username, role: admin|operator, enabled, password_hash (argon2id), created_at, last_login_at}`.
Nombre insensible a mayúsculas. Contraseña mínima 8 caracteres. Debe existir siempre al menos un
admin habilitado.

### 3.6 Credenciales de equipos (`CredentialStore`)

`CredentialStore.create(settings.credential_backend, paths.secrets_dir, secret_key)`.
API: `get_device_password(id) -> str` ("" si no hay), `set_device_password(id, pw)` ("" = borrar),
`delete_device_password(id)`, `has_device_password(id)`. Errores → `CredentialError`.
La API **nunca** devuelve contraseñas: solo `has_password: bool`.

---

## 4. Procesos, puertos y MediaMTX

### 4.1 Procesos y puertos por defecto

| Proceso | Puerto | Escucha | Notas |
|---|---|---|---|
| Backend VMS (`python -m vms`) | **8600/tcp** | `0.0.0.0` | REST, web, proxy WHEP y playback, SSE |
| MediaMTX RTSP | **8554/tcp** | `127.0.0.1` | lectura local (analítica). Para leer desde fuera: cambiar dirección + usuario lector (no por defecto) |
| MediaMTX WebRTC HTTP (WHEP) | **8889/tcp** | `127.0.0.1` | solo el backend habla con él (proxy) |
| MediaMTX WebRTC ICE | **8189/udp** (+ 8189/tcp opcional) | `0.0.0.0` | el vídeo va directo del MediaMTX al navegador; abrir en el firewall de Windows |
| MediaMTX API de control | **9997/tcp** | `127.0.0.1` | expone las URLs con credenciales: **nunca** fuera de localhost |
| MediaMTX playback | **9996/tcp** | `127.0.0.1` | `/list` y `/get`; la API lo sirve como proxy |
| MediaMTX métricas | **9998/tcp** | `127.0.0.1` | Prometheus |
| Analítica (`python -m analytics`) | — | — | lee RTSP local, escribe PostgreSQL, Telegram |
| Panel central (`python -m central`) | **8700/tcp** | `0.0.0.0` | servidor central, lee PostgreSQL |
| PostgreSQL | **5432/tcp** | servidor central | por la VPN |

HLS, RTMP, SRT, MoQ y pprof de MediaMTX: **desactivados**. En pruebas, todos los puertos se
piden libres (`tests.conftest.get_free_port`, fixture `settings`).

### 4.2 Rutas de MediaMTX por cámara (`vms.core.naming`)

```
<camera_id>/main   flujo principal → se GRABA 24/7. Conexión permanente al equipo.
<camera_id>/sub    subflujo → vista en vivo y analítica. sourceOnDemand (se abre al haber lectores).
```
- **Una sola conexión por flujo hacia el NVR/cámara**: MediaMTX reparte a todos los lectores
  (muros, reproducción no aplica, analítica). Verificado en `tests/e2e/test_infra_chain.py`.
- Sin subflujo (`has_sub=false`): vista en vivo y analítica usan `<camera_id>/main`.
- La vista en vivo usa `sub` por defecto; pantalla completa (1 celda) puede pedir `main`.
- `mtx_path(camera_id, stream)` y `parse_mtx_path(name)` son la única forma de construir/leer estos nombres.
- Las URLs de origen se construyen con `vms.core.sources.build_camera_sources(cfg, creds)`
  (presets Hikvision/Dahua, ruta manual para ONVIF/genérico; credenciales codificadas %XX).

### 4.3 Configuración generada y credenciales

> **v2 (§13.10, tras S2):** el YAML pasa a ser la **fuente única de las rutas** (modo `attach`, el motor es
> el servicio `VMSEngine`). Lo de abajo sigue valiendo para el modo `child` (desarrollo en macOS y Linux).

- `<datos>/mediamtx/mediamtx.yml` se genera en cada arranque **sin ninguna credencial**:
  direcciones de §4.1, protocolos desactivados, `authInternalUsers` **sin usuario anónimo**:
  `vms-backend` (`api`, `playback`, `read`, `metrics`) y `vms-reader` (`read`), ambos solo desde
  `127.0.0.1`/`::1`, **sin** `publish`, con usuario y contraseña escritos como hash `sha256:`
  (contraseñas derivadas por HMAC del token interno, `vms.core.mtx_auth`), `pathDefaults` de
  grabación y `paths: {}`. *(Cambiado en la revisión de seguridad; antes era el usuario `any`.)*
- Las rutas de cámaras se registran **por la API de control** (`POST /v3/config/paths/add/<camera_id>/main`
  …), así las contraseñas solo viven en memoria del proceso MediaMTX. Tras cualquier reinicio de
  MediaMTX el motor las vuelve a registrar. Cambios de cámara → `patch`/`delete` de rutas, sin reiniciar.
- `pathDefaults` de grabación: `rtspTransport` según cámara (por defecto `tcp`), `recordFormat: fmp4`,
  `recordPath: <recordings_dir>/%path/%Y-%m-%d_%H-%M-%S-%f%z` (con desfase horario: sin él, la
  hora repetida del cambio de hora de octubre era ambigua), `recordPartDuration: <part_seconds>s`,
  `recordSegmentDuration: <segment_seconds>s`, `recordDeleteAfter: <days*24>h`.
  Ruta `main`: `record: <camera.record>`; ruta `sub`: `record: false`, `sourceOnDemand: true`,
  `sourceOnDemandCloseAfter: 30s`.
- La salida estándar de MediaMTX se lee por tubería y se reenvía al logger `vms.engine.mediamtx`
  (así pasa por la ocultación de credenciales).
- Supervisor: si MediaMTX termina, se relanza con backoff (1, 2, 5, 10, 30 s) y se registran
  `restarts` y `last_error`. Si el puerto de la API ya está ocupado al arrancar → error claro.
  En Windows: `CREATE_NO_WINDOW`; al parar el backend, MediaMTX se para (no quedan huérfanos).
- **Disk guard** (motor): cada 60 s, si el volumen de grabación supera `disk_guard_percent`,
  borra los segmentos más antiguos (nunca el más reciente de cada ruta) hasta bajar del umbral y
  lo registra. Referencia de la lógica: `legacy/app/retention.py`.
- Verificado con MediaMTX v1.21.1: grabación con nombres `cam-xxx/main`, `/list` (devuelve
  `start` con desfase horario local) y `/get?format=mp4`. La API normaliza fechas a UTC (§6.1).

---

## 5. Interfaces internas (Python)

Definidas como `Protocol` en `vms/core/interfaces.py`. La API depende solo de ellas; en sus
pruebas usa `tests/fakes.py` (`FakeEngine`, `FakeDeviceClient`).

### 5.1 Fabricantes — `vms.vendors` [vendors-engine]

```python
def client_for(device: Device, password: str, *, timeout: float = 5.0) -> DeviceClient
async def test_device(device: DeviceBase, password: str, *, timeout: float = 5.0) -> DeviceTestResult
async def discover(timeout: float = 3.0, *, targets: list[tuple[str, int]] | None = None,
                   interfaces: list[str] | None = None) -> list[DiscoveredDevice]
```
- `DeviceClient` (Protocol): `vendor`, `probe() -> DeviceInfo`, `list_channels() -> list[ChannelInfo]`,
  `snapshot(channel, stream) -> bytes` (JPEG en memoria), `aclose()`.
- Hikvision: ISAPI con Digest (`/ISAPI/System/deviceInfo`, `/ISAPI/ContentMgmt/InputProxy/channels[/status]`,
  `/ISAPI/Streaming/channels[/<id>[/picture]]`). Dahua: CGI con Digest (`magicBox.cgi`,
  `configManager.cgi` `ChannelTitle`/`Encode`, `LogicDeviceManager.cgi getCameraState`,
  `snapshot.cgi`) y RPC2 si hace falta. Implementación propia, o adaptando código MIT
  (`rroller/dahua`) **con atribución** en el archivo y en `docs/TERCEROS.md`.
- ONVIF: `onvif-zeep-async` (GetDeviceInformation, GetProfiles, GetStreamUri, GetSnapshotUri).
  Para cámaras ONVIF, las rutas RTSP descubiertas se guardan en `Camera.main_path/sub_path`.
- WS-Discovery **propio** (UDP multicast 239.255.255.250:3702, Probe `dn:NetworkVideoTransmitter`);
  `targets` permite enviar el Probe a direcciones concretas (pruebas con `tools.mocks.wsdiscovery`).
  Fabricante deducido de los scopes (`hardware/`, `name/`) y del modelo.
- Errores: `DeviceUnreachable`, `DeviceAuthFailed`, `DeviceProtocolError`, `DeviceUnsupported`
  (mensajes en español). Ojo: muchas cámaras bloquean el usuario tras varios fallos; no reintentar
  credenciales malas en bucle.
- `test_device` nunca lanza: devuelve `DeviceTestResult{ok, reachable, auth_ok, rtsp_ok, info, channels, message}`.

### 5.2 Motor — `vms.engine` [vendors-engine]

> **v2 (§13.10):** `VMS_ENGINE_MODE=child|attach`. En `attach` el backend no lanza MediaMTX: escribe
> `mediamtx.yml` con `atomic_write` y usa la API solo para leer estado y como proxy WHEP/reproducción.

Clase prevista `MediaMtxEngine(settings: VmsSettings, paths: AppPaths)` que cumple `Engine`:
`start()`, `stop()`, `apply(sources, recording, retention, recordings_dir)` (idempotente,
sin reiniciar), `status()`, `paths_status()` (clave = nombre de ruta), `list_recordings(camera_id, start, end)`,
`playback_get_url(...)`, `whep_url(camera_id, stream)`, `rtsp_read_url(camera_id, stream)`, `disk_usage()`.
Errores de MediaMTX caído → `EngineUnavailable`.

### 5.3 Conexión motor ↔ API [api-web]

El backend, en su `lifespan`: configura logs → carga ajustes → `ConfigRepository` + `UserStore`
+ `CredentialStore` → `engine.start()` → `engine.apply(build_camera_sources(...), ...)` →
se suscribe a cambios de configuración (aplicación con antirrebote de 1 s) → arranca el latido
(§7.2) si hay `VMS_PG_DSN`. Al parar: latido → motor.

---

## 6. API REST del backend [api-web]

### 6.1 Convenciones

- Base `/api`. JSON UTF-8. Fechas **ISO 8601 en UTC con `Z`** en respuestas; en peticiones se
  aceptan con cualquier desfase. Duraciones en segundos (float).
- Errores: `{"error": {"code": "<estable>", "message": "<español>", "details": {...}}}` con el
  estado de `vms.core.errors` (`validation_error` 422 con `details.fields=[{loc, msg}]`,
  `unauthorized` 401, `forbidden` 403, `not_found` 404, `conflict` 409, `rate_limited` 429,
  `device_*` 502/501, `engine_unavailable`/`not_configured` 503).
- Sesión: cookie `vms_session` (aleatoria, `HttpOnly`, `SameSite=Strict`, `Secure` si HTTPS),
  almacenada en memoria del servidor; caduca a las `VMS_SESSION_HOURS` horas.
- CSRF: toda petición que modifica (`POST/PUT/PATCH/DELETE`) exige la cabecera
  `X-Requested-With: vms` (y no se habilita CORS). Sin ella → 403 `csrf`.
- Roles: **admin** (todo), **operator** (ver, vivo, reproducción, cambiar muros), **kiosk**
  (sesión de operador limitada a: muros, cámaras, vivo, eventos; solo lectura).
  En la tabla: A = admin, O = operador o superior, K = también kiosco, I = token interno
  (`X-VMS-Internal-Token`), — = sin sesión.
- Contraseñas en peticiones: nunca se registran ni se devuelven.

### 6.2 Autenticación y usuarios

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `POST /api/auth/login` | — | `{username, password}` → 200 `{user: UserPublic}` + cookie. 401 `invalid_credentials`. 5 fallos/5 min por IP+usuario → 429 con `Retry-After`. |
| `POST /api/auth/logout` | K | → 204 |
| `GET /api/auth/me` | K | → `{username, role, kiosk: bool}` |
| `GET /api/auth/setup` | — | → `{needed: bool}` (no hay usuarios) |
| `POST /api/auth/setup` | — | solo si no hay usuarios **y** desde 127.0.0.1/::1: `{username, password}` → 201 admin creado. Si no, 403/409. |
| `GET /api/auth/kiosk?token=…&next=/wall/1` | — | token = `VMS_KIOSK_TOKEN`, solo desde localhost salvo `VMS_KIOSK_ALLOW_REMOTE` → 303 a `next` (ruta relativa interna) con cookie de kiosco. |
| `GET /api/users` | A | → `[UserPublic]` |
| `POST /api/users` | A | `UserCreate` → 201 `UserPublic`; 409 si existe |
| `PATCH /api/users/{username}` | A | `UserUpdate` → `UserPublic`; 409 si deja el sistema sin admin habilitado |
| `DELETE /api/users/{username}` | A | → 204; 409 si es el último admin o es uno mismo |

### 6.3 Equipos (dispositivos)

`DeviceOut = Device + {has_password: bool, cameras: [camera_id], online: bool|null}`.

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/devices` | O | → `[DeviceOut]` |
| `POST /api/devices` | A | `DeviceCreate` → 201 `DeviceOut`. Si `import_channels` (lista o `"all"`), consulta canales y crea cámaras (nombre del canal). Si falla la consulta, el equipo se crea igual y `details.import_error` lo explica. |
| `GET /api/devices/{id}` | O | → `DeviceOut` |
| `PATCH /api/devices/{id}` | A | `DeviceUpdate` → `DeviceOut` (`password` null = sin cambio, "" = borrar) |
| `DELETE /api/devices/{id}` | A | → 204 (cascada §3.3 + borra contraseña) |
| `POST /api/devices/test` | A | `DeviceTestRequest` (sin guardar) → `DeviceTestResult` (siempre 200) |
| `POST /api/devices/{id}/test` | A | → `DeviceTestResult` con la contraseña guardada |
| `GET /api/devices/{id}/channels` | A | → `[ChannelInfo]` (502 si el equipo falla) |
| `POST /api/devices/{id}/channels/import` | A | `{channels: [int] \| "all"}` → 201 `[Camera]` (omite canales ya dados de alta) |
| `POST /api/discovery/scan` | A | `{timeout_s: 1..10 = 3}` → `{devices: [DiscoveredDevice]}` (`already_added` según host) |

### 6.4 Cámaras

`CameraOut = Camera + {device_name, vendor, live: {online, recording, readers, tracks, codec_warning}}`
(`codec_warning` = texto si el subflujo no es H.264, p. ej. «El subflujo es H.265: el navegador
no lo reproduce por WebRTC. Cámbialo a H.264 en el NVR»).

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/cameras` | K | → `[CameraOut]` |
| `POST /api/cameras` | A | `CameraCreate` → 201 `CameraOut` (409 si ese equipo+canal ya existe) |
| `GET /api/cameras/{id}` | K | → `CameraOut` |
| `PATCH /api/cameras/{id}` | A | `CameraUpdate` → `CameraOut` |
| `DELETE /api/cameras/{id}` | A | → 204 (cascada) |
| `GET /api/cameras/{id}/snapshot?stream=sub\|main` | A | → `image/jpeg`, `Cache-Control: no-store`. Del equipo (`DeviceClient.snapshot`); si falla y hay OpenCV, un frame del RTSP local. **Nunca se guarda en disco.** Máx. 1 petición/s por cámara (429). |

### 6.5 Muros (monitores)

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/walls` | K | → `[WallLayout]` (4) |
| `GET /api/walls/{monitor}` | K | → `WallLayout` con `ETag: "<revision>"` |
| `PUT /api/walls/{monitor}` | O | `WallUpdate` → `WallLayout` (ids inexistentes → 422) |

### 6.6 Vista en vivo (proxy WHEP)

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/live/{camera_id}?stream=sub\|main` | K | → `{camera_id, stream, whep_url: "/api/live/{id}/{stream}/whep", ready, tracks}` |
| `OPTIONS /api/live/{id}/{stream}/whep` | K | reenvía (cabeceras `Link` de servidores ICE) |
| `POST /api/live/{id}/{stream}/whep` | K | `application/sdp` (oferta) → 201 `application/sdp` (respuesta). `Location` reescrito a `/api/live/{id}/{stream}/whep/{session}` conservando el id de sesión de MediaMTX. |
| `PATCH /api/live/{id}/{stream}/whep/{session}` | K | `application/trickle-ice-sdpfrag` → 204 |
| `DELETE /api/live/{id}/{stream}/whep/{session}` | K | → 200 |

Excepción CSRF: estas rutas exigen igualmente `X-Requested-With: vms` (el lector JS la añade).
El cliente JS puede partir del lector WHEP de MediaMTX (MIT; copiar a `vms/web/vendor/` con su
licencia). El vídeo viaja directo al puerto ICE (§4.1).

### 6.7 Grabaciones y reproducción

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/recordings/{camera_id}/timeline?start=&end=` | O | → `{camera_id, spans: [{start, end, duration}]}` (UTC, envuelve `/list`) |
| `GET /api/recordings/{camera_id}/video?start=&duration=&format=fmp4\|mp4&download=0\|1` | O | → `video/mp4` en streaming (proxy de `/get`). `duration` ≤ 3600. `download=1` añade `Content-Disposition` con nombre `<cámara>_<AAAAMMDD-HHMMSS>.mp4`. 404 si no hay grabación. |
| `GET /api/recordings/summary` | O | → `[{camera_id, first, last, bytes}]` |

### 6.8 Estado y salud

| Método y ruta | Rol | Respuesta |
|---|---|---|
| `GET /api/health` | — | `{status: ok\|degraded\|down, version, uptime_s, engine: {running, api_ok}}` (sin datos sensibles; lo usa el kiosco y el instalador) |
| `GET /api/status` | O | `{engine: EngineStatus, disk: DiskUsage, cameras: [{camera_id, name, online, recording, readers, bytes_received, last_error}], analytics: <§8.6 + stale>, credential_backend, config_warning}` |

`status`: `down` si el motor no corre; `degraded` si hay cámaras sin vídeo, disco por encima
del umbral o analítica activada sin estado reciente; si no, `ok`.

### 6.9 Ajustes

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/settings` | O | → `SystemSettings` |
| `PATCH /api/settings` | A | parcial de `SystemSettings` → `SystemSettings` |
| `GET /api/settings/retention` | O | → `RetentionSettings` |
| `PUT /api/settings/retention` | A | `RetentionSettings` → `RetentionSettings` (el motor lo aplica en caliente) |

### 6.10 Analítica

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/analytics/rules?camera_id=` | O | → `[LineRule \| ZoneRule]` |
| `POST /api/analytics/rules` | A | regla (sin `id`) → 201 regla |
| `GET /api/analytics/rules/{id}` | O | → regla |
| `PUT /api/analytics/rules/{id}` | A | regla completa → regla (no puede cambiar `kind` ni `camera_id`: 422) |
| `DELETE /api/analytics/rules/{id}` | A | → 204 |
| `GET /api/analytics/cameras` | O | → `[CameraAnalytics]` |
| `PUT /api/analytics/cameras/{camera_id}` | A | `CameraAnalytics` → `CameraAnalytics` |
| `GET /api/analytics/status` | O | → contenido de `analytics/status.json` + `stale: bool` (> 30 s) ; si no existe → `{running: false}` |
| `GET /api/internal/analytics/config` | I | → §8.2 (con `ETag`; `If-None-Match` → 304) |

### 6.11 Eventos en tiempo real

`GET /api/events` (K) — Server-Sent Events:
- `event: config` · `data: {"revision": 13, "scope": "devices|cameras|walls|settings|analytics|users"}`
- `event: status` (cada 5 s) · `data: {"cameras": [{"camera_id", "online", "recording"}], "engine": {"running"}}`
- comentario `: ping` cada 15 s. Los muros recargan su layout al recibir `config` con scope `walls`/`cameras`.

### 6.12 Páginas web (servidas por el backend)

`/` panel · `/login` · `/setup` (primer arranque, solo localhost) · `/wall/{1..4}` muro a
pantalla completa (doble clic en celda = pantalla completa con `main`; sin sesión → `/login`) ·
`/playback` línea de tiempo · `/analytics` dibujo de líneas y zonas sobre el snapshot ·
`/status` estado del sistema · `/static/...`. Sin CDN externos en las páginas de los muros (deben
funcionar sin Internet).

El backend las sirve con `vms.web.mount_web(app)` (registra estas rutas y monta `/static`), o
leyendo `vms.web.PAGES` / `STATIC_DIR`. Las páginas no exigen sesión al servirse: cada una llama a
`GET /api/auth/me` y, con 401, redirige a `/login?next=<ruta>` (solo rutas internas).

---

## 7. PostgreSQL, latido y panel central

### 7.1 Esquema

Migración inicial: `vms/db/migrations/0001_initial.sql` (aplicar con `python -m vms.db.migrate`
o `vms-migrate`; idempotente, con bloqueo consultivo). Tablas:

| Tabla | Clave | Contenido |
|---|---|---|
| `sites` | `site_id` | nombre, código, zona horaria, activa |
| `site_cameras` | `(site_id, camera_id)` | nombre de cámara (sin URLs ni credenciales) |
| `analytics_rules` | `(site_id, rule_id)` | `camera_id`, `kind` line/zone, `name`, `config` jsonb (geometría y umbrales), `active` |
| `line_counts_minute` | `(site_id, rule_id, minute)` | `count_in`, `count_out` (minuto truncado, UTC) |
| `zone_occupancy_minute` | `(site_id, rule_id, minute)` | `samples`, `avg_people`, `max_people`, `seconds_over_threshold` (0..60) |
| `queue_alerts` | `alert_id uuid` | `rule_id`, `camera_id`, `started_at`, `ended_at`, `peak_people`, `threshold`, `notified_at`, `notify_error` |
| `site_heartbeats` | `site_id` | `last_seen`, `hostname`, `version`, `status` ok/degraded/down, `payload` jsonb |
| `weekly_reports` | `(site_id, week_start)` | lunes ISO; `status` ok/error, `provider`, `model`, `metrics` jsonb, `body_markdown`, `error` |

Escritura idempotente: los minutos se escriben **una vez al cerrarse** con
`INSERT … ON CONFLICT DO UPDATE SET <cols> = EXCLUDED.<cols>` (reintentar no duplica).
Antes de escribir conteos de una sede hay que asegurar su fila en `sites` (upsert).
Cambios de esquema: nueva migración `NNNN_….sql`, nunca editar una ya publicada.
`0002_site_row_security`: tabla `site_db_roles (role_name, site_id)`, función
`vms_session_site()` y política `site_rows` (RLS) en las 8 tablas con `site_id`.
Roles en producción: **un rol por tienda** (`python -m vms.db.site_roles create <sede>`:
SELECT/INSERT/UPDATE en sites, site_cameras, analytics_rules, conteos, alertas y latidos, y RLS
limita todo a su `site_id`) y `vms_central` (SELECT + weekly_reports; sin sede = ve todas).

### 7.2 Latido de sede [central-deploy implementa, api-web lo arranca]

```python
# central/heartbeat.py
class HeartbeatSender:
    def __init__(self, dsn: str, site: Site, interval_s: int,
                 collect: Callable[[], Awaitable[dict[str, Any]]]) -> None: ...
    async def start(self) -> None: ...   # tarea en segundo plano; nunca lanza hacia fuera
    async def stop(self) -> None: ...
```
Cada `interval_s`: upsert de `sites`, de `site_cameras` (desde `payload.cameras`) y de
`site_heartbeats`. Si PostgreSQL no responde, registra aviso y reintenta en el siguiente ciclo
(sin acumular). Usa `psycopg` asíncrono.

### 7.3 `payload` del latido (lo construye `collect` en el backend)

```json
{"version": "0.1.0", "hostname": "TIENDA-001", "uptime_s": 86400, "status": "ok",
 "engine": {"running": true, "restarts": 0},
 "cameras": [{"camera_id": "cam-1a2b3c4d", "name": "Puerta", "online": true, "recording": true}],
 "cameras_total": 12, "cameras_online": 12,
 "disk": {"percent": 63.2, "free_gb": 812.4},
 "analytics": {"running": true, "stale": false}}
```

### 7.3 bis Campos del latido que aportan los bloques de la v2

`vms/core/heartbeat_extras.py` (dueño: arquitecto) declara qué clave añade cada bloque y de dónde sale; el
backend (`AppState.heartbeat_payload`) y el agente (`central/agent.py`) ya la recogen desde la fase 0:

| Clave | Proveedor (`módulo:función`) | Bloque | Contenido |
|---|---|---|---|
| `update` | `vms_updater.heartbeat:payload_update` | B4 | campos de `public-status.json` (§15.6) |
| `health` | `vms.ops.heartbeat:payload_health` | B6 | §18.5 |
| `evidence_key` | `vms.ops.heartbeat:payload_evidence_key` | B6 | `{"key_id", "public_key"}` (§18.7) |

Un proveedor es `def f(paths: AppPaths) -> dict | None`, síncrono y rápido: solo lee archivos locales (nunca
red ni base de datos), sin imágenes, IP ni secretos. Si el módulo aún no existe, lanza, o devuelve algo que
no es un objeto JSON de ≤ 8 KB, esa clave no se envía y el resto del latido sale igual. `HeartbeatPayload`
admite campos extra, así que nadie toca `central/heartbeat.py`. Una clave nueva se pide por §12.

### 7.4 Panel central [central-deploy]

`python -m central` (FastAPI, puerto 8700, usuarios propios con el mismo esquema que §3.5 en su
carpeta de datos). Lee solo PostgreSQL. Propuesta mínima de API (el agente puede ampliarla
documentándolo en §12): `GET /api/sites` (con último latido y estado; sede sin latido > 3
intervalos = `down`), `GET /api/sites/{id}/counts?from=&to=&bucket=hour|day`,
`GET /api/sites/{id}/queue-alerts?from=&to=`, `GET /api/sites/{id}/reports`,
`GET /api/sites/{id}/reports/{week_start}`. Mismas convenciones de §6.1.

---

## 8. Analítica [analytics]

### 8.1 Proceso

`python -m analytics` en la sede (servicio aparte del backend). Sin GUI. Obtiene su
configuración del backend (§8.2) cada 30 s con `If-None-Match`; si el backend no responde,
sigue con `analytics/config-cache.json`. Importar `analytics` deja `sys.modules["av"] = None`
(PyAV prohibido).

### 8.2 Configuración (`GET /api/internal/analytics/config`)

```json
{"revision": 12,
 "site": {"id": "site-bcn-001", "name": "Tienda Gràcia", "timezone": "Europe/Madrid"},
 "alerts": {"telegram_enabled": true, "telegram_chat_id": "-1001234567890"},
 "cameras": [
   {"camera_id": "cam-1a2b3c4d", "name": "Puerta", "rtsp_url": "rtsp://127.0.0.1:8554/cam-1a2b3c4d/sub",
    "fps": 12.0, "detector": "rfdetr-nano", "confidence": 0.5,
    "rules": [ {"kind": "line", "id": "rule-…", "camera_id": "…", "name": "Entrada", "enabled": true,
                "start": [0.1, 0.55], "end": [0.9, 0.55], "invert": false, "updated_at": "…"} ]}
 ]}
```
Solo cámaras con analítica activada y al menos una regla activa. `rtsp_url` es local y sin
credenciales (`engine.rtsp_read_url`). El token de Telegram **no** viaja: la analítica lo lee
de su propio entorno (`VMS_TELEGRAM_BOT_TOKEN`).

### 8.3 Frames

- Lectura RTSP por TCP del subflujo **desde MediaMTX** (nunca directo al NVR: una sola conexión por canal).
- `cv2.VideoCapture` (opencv-python-headless, FFmpeg LGPL en Windows/Linux) en un hilo por
  cámara con patrón «último frame»; se procesa a `fps` de la cámara (puerta 10-15, cajas 1-2)
  descartando el resto. Reconexión con backoff (1, 2, 5, 10, 30 s).
- El frame se procesa en memoria y se descarta. **Prohibido** escribir frames, recortes o
  vídeo a disco o a la base de datos, también en modo depuración.

### 8.4 Detección y seguimiento

- RF-DETR **nano/small/medium/base** (Apache-2.0), exportado a ONNX y ejecutado con
  OpenVINO (CPU Intel N150/i5) u ONNX Runtime. En la sede **no** se instala torch
  (extra `analytics`); exportar el modelo es una herramienta aparte (extra `export`) que deja
  los archivos en `models/` (ignorados por git). Solo clase persona (COCO `person`).
- Seguimiento con `trackers` (ByteTrack, Apache-2.0). `sv.ByteTrack` está deprecado: no usarlo.
- Línea: `supervision.LineZone` (o equivalente propio) respetando la semántica de `LineRule`
  (docstring en `vms/core/models.py`: entrada = paso de s<0 a s>0; `invert` intercambia).
  Punto de referencia por defecto: centro inferior de la caja.
- Zona: `supervision.PolygonZone`; ocupación = personas seguidas con el punto de referencia dentro.
- Coordenadas de reglas normalizadas 0..1 → píxeles con la resolución real del flujo.

### 8.5 Agregación, persistencia y alertas

- Minuto UTC: línea → `count_in/out`; zona → `samples`, `avg_people`, `max_people`,
  `seconds_over_threshold`. Se escribe al cerrar el minuto (+5 s de margen), idempotente (§7.1).
- Si PostgreSQL falla: cola en `analytics/spool/*.jsonl` y reenvío ordenado al volver
  (mismos upsert). Nunca se pierde un minuto cerrado por un corte de red.
- Al cambiar la revisión: upsert de `sites`, `site_cameras`, `analytics_rules`.
- Alerta de cola: ocupación ≥ `alert_threshold` durante `alert_min_seconds` seguidos y
  pasado `alert_cooldown_seconds` desde la anterior → fila en `queue_alerts` (uuid4) +
  Telegram (`sendMessage`, texto en español, **sin imágenes**). Fin: ocupación ≤ `clear_below`
  (por defecto umbral−1) durante 30 s → `ended_at`. Fallo de Telegram → `notify_error`, reintento
  con backoff, nunca bloquea el conteo.

### 8.6 Estado (`<datos>/analytics/status.json`, escritura atómica cada 10 s)

```json
{"running": true, "updated_at": "2026-10-04T13:40:00Z", "pid": 1234, "version": "0.1.0",
 "config_revision": 12,
 "db": {"ok": true, "spool_pending": 0, "last_error": ""},
 "cameras": [{"camera_id": "cam-1a2b3c4d", "state": "running|connecting|error", "fps_in": 12.1,
              "fps_processed": 11.8, "inference_ms_p50": 38.5, "last_error": "",
              "occupancy": {"rule-…": 3}}]}
```
Campos adicionales (aditivos, la API puede ignorarlos): `uptime_s`, `config_error`,
`telegram{enabled, token_configured, sent}` y, por cámara, `name`, `fps_target`,
`inference_ms_p95`, `frames_processed`, `reconnects`, `detector{model, backend}`, `frame_size`
y `totals{rule_id: {in, out}}` (acumulados desde el arranque, solo informativos).

### 8.7 Informe semanal

Se ejecuta en el servidor central (programado lunes 06:00 Europe/Madrid, semana ISO anterior).
Código en `analytics/reports/` sin dependencias de visión (lo usa el extra `central`).
1) Cifras calculadas en SQL (fuente de verdad, se guardan en `metrics`): entradas/salidas por
día y hora, hora punta, alertas de cola (número y duración), ocupación media por franja,
comparación con la semana anterior. 2) El proveedor LLM **solo redacta** a partir de esas
cifras (instrucción explícita de no inventar; se valida que los números citados existan en
`metrics`). 3) Interfaz:
```python
class LlmProvider(Protocol):
    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1500) -> str: ...
def provider_from_settings(settings: VmsSettings) -> LlmProvider | None   # "anthropic" | "none"
```
Si el proveedor es `none` o falla: informe solo con cifras y `status=error` + `error` (sin
datos personales; a la API del proveedor solo viajan agregados).
**Implementado así** (ver §12): con `none` (o sin clave) el texto sale de una plantilla fija y se
guarda `status=ok`, `provider=template` (no es un error, es la configuración elegida). Si el
proveedor configurado falla o cita números que no están en `metrics`, se usa la plantilla y se
guarda `status=error` con el motivo en `error`. Funciones públicas: `analytics.reports.
generate_weekly_report(dsn, site_id, week_start, provider, store=True) -> WeeklyReport`,
`previous_iso_week_start()`, `list_active_sites(dsn)` y la CLI `python -m analytics.reports`.
`body_markdown` guarda el informe completo en Markdown (resumen + tablas de cifras).

### 8.8 Pedidos al núcleo [analytics → api-web]

Lo que la analítica necesita del backend y aún no existe en `vms/` (lo conecta el integrador):

1. **`GET /api/internal/analytics/config`** (§6.10, rol I) debe devolver el documento §8.2. Para
   no duplicar la lógica, el backend puede usar `analytics.config.build_analytics_config(cfg,
   revision, settings.site_id, rtsp_read_url)` con `rtsp_read_url = engine.rtsp_read_url` (o
   `analytics.config.default_rtsp_read_url(settings.mtx_rtsp_address)`). Solo depende de
   `vms.core` y pydantic. `ETag` = `"<revision>"`; con `If-None-Match` igual → 304. La analítica
   ya maneja 200/304/401/caída (probado con transporte simulado).
2. **Snapshot de respaldo** (§6.4): si `DeviceClient.snapshot` falla, el backend puede llamar a
   `analytics.snapshot.grab_jpeg(rtsp_url)` (bloqueante: usar `asyncio.to_thread`); devuelve bytes
   JPEG en memoria o lanza `DeviceUnreachable` con mensaje en español. Nunca escribe en disco.
3. **`GET /api/analytics/status`**: leer `<datos>/analytics/status.json` tal cual (§8.6) y añadir
   `stale` (> 30 s desde `updated_at`). Los campos extra son compatibles.
4. **Despliegue**: `python -m analytics` como servicio aparte (`vms-analytics.service` / servicio
   de Windows) con la misma `VMS_DATA_DIR` y `.env` que el backend; `python -m analytics check`
   sirve de diagnóstico del instalador. Copiar `models/rfdetr-nano.{onnx,xml,bin,json}` a
   `<instalación>/models/` (o fijar `VMS_ANALYTICS_MODELS_DIR`).

---

## 9. Infraestructura de pruebas compartida

### 9.1 Fixtures (`tests/conftest.py`)

| Fixture | Qué da |
|---|---|
| `app_paths`, `settings`, `credential_store`, `config_repo` | carpeta de datos aislada, ajustes con puertos libres, almacén cifrado, repositorio |
| `free_port` / `get_free_port()` | puerto TCP libre |
| `mediamtx_bin`, `ffmpeg_bin`, `ffprobe_bin` | rutas o `skip` explicado |
| `people_video` | `tests/assets/people-walking-h264.mp4` (1280x720, 15 fps, H.264 baseline sin B-frames) |
| `pg_server_dsn` | servidor PostgreSQL 16 (pgserver embebido, o `VMS_TEST_PG_DSN`) |
| `pg_dsn` | **base nueva por prueba**, ya migrada (clon de plantilla) |
| `pg_empty_dsn` | base nueva vacía (para probar migraciones) |
| `camsim_factory`, `camsim` | simulador de cámaras (§9.2); `camsim` = `hik1` (2 canales) + `dah1` (2 canales) |
| `hik_mock`, `dahua_mock` | mocks ASGI (§9.3) |
| `asgi_client(app, auth=...)` | cliente httpx sin red contra un mock |
| `mock_server(app)` | sirve un mock en puerto real (`.base_url`, `.port`) |

Además el `conftest` borra las variables `VMS_*` del entorno, usa keyring en memoria y manda
los temporales a `.tmp/pytest`. Dobles: `tests/fakes.py` (`FakeEngine`, `FakeDeviceClient`).

### 9.2 Simulador de cámaras (`tools/camsim`)

- `CameraSimulator([SimDevice(name, vendor, channels, videos={canal: Path}), ...], workdir)`:
  MediaMTX interno + ffmpeg (testsrc2 con nombre/canal/reloj, o vídeo en bucle) + un proxy
  RTSP por equipo en su propio puerto que **imita las rutas nativas y exige Digest**:
  `rtsp://admin:<pw>@127.0.0.1:<puerto>/Streaming/Channels/101` · `/cam/realmonitor?channel=1&subtype=0` · `/ch1/main`.
  Contraseña por defecto con caracteres conflictivos (`Sim#Pass:1@/x`).
- Motivo del proxy: **MediaMTX descarta la query string al resolver rutas** (comprobado:
  `cam/realmonitor?channel=1` y `?channel=2` caen en la misma ruta). El proxy reescribe URLs
  en peticiones, `Content-Base`, `RTP-Info` y SDP. Solo transporte TCP.
- Control: `kill_stream(dev, ch, "main")`, `start_stream(...)`, `restart_stream(...)`,
  `set_device_online(dev, False/True)`, `proxy_connections(dev)`, `internal_url(...)`.
- CLI: `python -m tools.camsim --hikvision hik1:4 --dahua dah1:4 [--video 1=tests/assets/people-walking-h264.mp4]`
  (órdenes por teclado: `kill|start <equipo> <canal> <main|sub>`, `off|on <equipo>`, `urls`, `quit`).
- Vídeo en bucle a cualquier URL: `python -m tools.publish_video --video … --url rtsp://…`.

### 9.3 Mocks de fabricantes (`tools/mocks`)

- `HikvisionMock(kind="nvr"|"camera")` — ISAPI con Digest, XML ver20 real, 4 canales por
  defecto (uno offline, uno H.265), snapshot JPEG, 404 `notSupport`.
- `DahuaMock(kind=...)` — CGI con Digest (texto CRLF) + RPC2 con login por desafío.
- `OnvifMock(base_url, rtsp_base)` — Device/Media con WS-Security PasswordDigest; probado con
  el cliente real `onvif-zeep-async`.
- `WsDiscoveryResponder([ProbeTarget(...)])` + `probe_match_xml`, `hikvision_scopes()`, `dahua_scopes()`.
- Los agentes pueden ampliar los mocks (nuevos endpoints) sin romper los existentes.

### 9.4 Reglas

Puertos siempre libres y dinámicos. Ningún proceso huérfano (paradas en `finally`/fixtures).
Nada fuera del proyecto y del scratchpad. Las pruebas que necesitan binarios o red se
saltan con un motivo claro, no fallan de forma críptica.

---

## 10. Licencias y hallazgos verificados

| Pieza | Licencia | Estado |
|---|---|---|
| MediaMTX v1.21.1 | MIT | binario oficial verificado por SHA-256; redistribuir con `MEDIAMTX-LICENSE.txt` |
| FastAPI, Starlette, uvicorn, httpx, pydantic | MIT/BSD | ok |
| onvif-zeep-async, zeep | MIT | ok |
| psycopg 3 | LGPL-3 (se usa sin modificar, como biblioteca importada; `psycopg-binary` incluye libpq con licencia PostgreSQL) | ok |
| keyring, cryptography, argon2-cffi | MIT / Apache-BSD / MIT | ok |
| supervision | MIT | ok, **pero declara PyAV** (ver abajo) |
| trackers | Apache-2.0 | ok (ByteTrack) |
| RF-DETR (código y pesos N/S/M/B) | Apache-2.0 | ok. **XL/2XL = PML: prohibidos** |
| OpenVINO, ONNX Runtime | Apache-2.0 / MIT | ok |
| opencv-python-headless | Apache-2.0 + FFmpeg | ver abajo |
| anthropic (SDK del proveedor LLM) | MIT | solo servidor central |

**Prohibidos**: Ultralytics YOLO y pesos `.pt` (AGPL), python-amcrest (GPL), PyAV de PyPI
(FFmpeg GPL con x264/x265), imageio-ffmpeg, mpv/python-mpv, ZoneMinder, Shinobi, Moonfire,
Bluecherry (ni su código), SDK propietarios Hikvision/Dahua, PyQt. PySide6 solo con enlace
dinámico (hoy no se usa).

**Hallazgos verificados al montar el entorno (4-oct-2026):**
1. `supervision 0.30.7` declara `av>=14.2` (PyAV), aunque solo lo importa dentro de sus
   utilidades de vídeo. PyAV se **desinstaló** y los locks lo excluyen: instalar siempre con
   `--no-deps`. La analítica no debe usar `sv.VideoInfo`, `sv.get_video_frames_generator` ni
   `sv.VideoSink`; lee con OpenCV.
2. `opencv-python-headless 5.0.0.93`: las wheels de **Windows** (`opencv_videoio_ffmpeg*.dll`) y
   **Linux** (`libavcodec` en `opencv_python_headless.libs`) traen FFmpeg **LGPL 2.1** (comprobado
   en los binarios). La wheel de **macOS arm64** trae el FFmpeg de Homebrew compilado con
   `--enable-gpl` (libx264/libx265): **solo desarrollo, nunca se distribuye un build de macOS**.
   `tests/test_licenses.py` lo comprueba en Windows/Linux.
3. `trackers` declara `opencv-python`; se sustituye por `opencv-python-headless` (mismo módulo `cv2`).
4. `torch` en Linux arrastra las wheels CUDA de NVIDIA (varios GB): por eso la inferencia en la
   sede va sin torch (extra `analytics`) y la exportación del modelo va aparte (extra `export`).
5. `matplotlib` (FreeType «FTL OR GPL-2.0», se elige FTL) y `scipy` (libgfortran «GPL-3.0 WITH
   GCC-exception-3.1») son excepciones revisadas, no copyleft para nosotros.

Al redistribuir: incluir `THIRD-PARTY-NOTICES` generado con `pip-licenses` (tarea de deploy) y
los textos LGPL (FFmpeg incluido en OpenCV y psycopg) con la oferta de código fuente que exige la LGPL.

---

## 11. Despliegue [central-deploy]

- **Windows (PC de control y tiendas Windows)**: backend como servicio de Windows (WinSW, MIT)
  ejecutando el Python embebido oficial (licencia PSF) con las wheels del lock; MediaMTX en
  `<instalación>\bin`; datos en `%PROGRAMDATA%\VMSMultimarca`; reglas de firewall para 8600/tcp y
  8189/udp(+tcp); `icacls` sobre `secrets\`. Instalador con Inno Setup (o equivalente con
  licencia que permita uso comercial). Descarga de MediaMTX con `tools/fetch_mediamtx.py --platform windows_amd64`.
- **Kiosco 4 monitores**: script que detecta la geometría de cada pantalla y abre Edge/Chrome
  `--kiosk` (un `--user-data-dir` por monitor, `--window-position`) en
  `http://127.0.0.1:8600/api/auth/kiosk?token=…&next=/wall/N`; relanza la ventana si se cierra;
  arranque con la sesión (`vms.core.autostart` o tarea programada).
- **Linux (mini PC de tienda)**: unidades systemd `vms.service` y `vms-analytics.service`
  (usuario `vms`, `StateDirectory=vms-multimarca`, `VMS_DATA_DIR=/var/lib/vms-multimarca`,
  `Restart=always`), `credential_backend=file` (no hay llavero sin sesión gráfica).
- Lo que no se pueda probar en macOS (instalador, servicio Windows, PowerShell) se entrega con
  validación estática y se documenta como no probado.

---

## 12. Cambios

| Fecha | Quién | Cambio |
|---|---|---|
| 2026-10-04 | arquitecto | Versión 1.0 inicial. |
| 2026-10-04 | interfaz web | §6.12: nueva página `/status`; `vms.web.mount_web(app)` para servir páginas y estáticos (aditivo). Lo que la interfaz espera de la API, sin cambiar nada de §6: (1) `POST /api/devices` con fallo de importación devuelve 201 y el motivo en `details.import_error` **dentro del cuerpo** `DeviceOut`; (2) el `POST` WHEP responde **404** cuando la ruta no tiene vídeo (el muro muestra «Sin señal» y reintenta) y 503/502 si el motor no responde; (3) los errores 422 usan `details.fields[].loc` con el nombre del campo (p. ej. `["body","host"]`) para marcarlo en el formulario; (4) recomendación: las sesiones de **kiosco** deberían renovarse mientras el muro está abierto (caducidad deslizante); si caducan a las `VMS_SESSION_HOURS`, el muro cae en `/login` y hace falta que el lanzador del kiosco vuelva a abrir la URL con el token. |
| 2026-10-04 | analytics | `pyproject.toml` extra `export`: añadido `onnx>=1.16` (Apache-2.0); `torch.onnx.export` lo exige al exportar RF-DETR. Locks regenerados con `tools.lock_requirements` (solo cambian export y dev: `onnx`, `ml-dtypes`). |
| 2026-10-04 | analytics | `analytics/__init__.py` bloquea también `openvino_telemetry`: `import openvino` enviaba un evento de uso a Google Analytics (comprobado con OpenVINO 2026.4: el contador de `~/intel/stats` subía en cada importación). Con el bloqueo, lectura de ONNX/IR e inferencia funcionan igual. |
| 2026-10-04 | analytics | OpenVINO se ejecuta con `INFERENCE_PRECISION_HINT=f32` por defecto (`VMS_ANALYTICS_OPENVINO_PRECISION`): en CPU ARM su valor por defecto es f16 y detectaba la mitad de personas que el modelo original. |
| 2026-10-04 | analytics | Nuevas variables opcionales `VMS_ANALYTICS_*` (`analytics.settings.AnalyticsSettings`; documentadas al final de `.env.example`). `.gitignore`: añadido `models/weights/` (pesos `.pth` que descarga la exportación; antes iban a `~/.roboflow`, ahora dentro del proyecto con `RF_HOME`). |
| 2026-10-04 | analytics | §8.2: `site` admite además `code` (opcional). §8.6: campos extra aditivos en `status.json`. §8.7: con proveedor `none` el informe se guarda `status=ok`, `provider=template` (el error se reserva a fallos reales del proveedor). Nueva §8.8 «Pedidos al núcleo». |
| 2026-10-04 | central-deploy | §7.2–7.4 ampliado (aditivo): además de `HeartbeatSender` (escritura directa, sin cambios de firma), latido **HTTP**: `POST /api/heartbeat` en el panel central con `Authorization: Bearer <token de sede>` y cuerpo `{site: {id, name?, code?, timezone?}, payload: <§7.3>}`; lo envía el agente `python -m central.agent` (lee `/api/health` sin sesión y, si hay `VMS_AGENT_USERNAME/PASSWORD` de un **operador**, `/api/status` y `/api/settings`). Ambos caminos usan `central.heartbeat.record_heartbeat()` y `last_seen = now()` del servidor. El payload admite además `interval_s`, `temperature_c`, `backend_reachable`, `agent_version`; un `status` desconocido se guarda como `degraded`. Tokens: solo SHA-256 en `<datos central>/config/site_tokens.json`. |
| 2026-10-04 | central-deploy | §7.4 API del panel ampliada (aditivo): `GET /api/health`, `/api/auth/*` (como §6.2, cookie `vms_central_session`), `GET /api/sites/{id}`, `/occupancy`, `/queue-alerts`, `GET /api/compare`, `GET /api/queues/top`, `GET /api/reports/latest`, `/api/site-tokens[/{site_id}]` (A), `/api/users` (A). Parámetro `range=today\|yesterday\|week\|prev_week\|last7\|last30` (cortes en la zona horaria de la sede) además de `from`/`to`. Detalle en `docs/PANEL-CENTRAL.md`. |
| 2026-10-04 | central-deploy | Variables nuevas (`.env.example`, sección final): `VMS_CENTRAL_*` (`central.settings.CentralSettings`; `VMS_CENTRAL_PG_DSN` cae en `VMS_PG_DSN`) y `VMS_CENTRAL_URL`, `VMS_SITE_TOKEN`, `VMS_AGENT_*` (`AgentSettings`). §7.1 roles: `vms_central` necesita además INSERT/UPDATE en `sites`, `site_cameras`, `site_heartbeats` si recibe latidos HTTP. |
| 2026-10-04 | central-deploy | §11 despliegue: **MediaMTX no se registra como servicio propio** (ni en Windows ni en systemd): lo supervisa el backend (§4.3/§5.2); un servicio aparte ocuparía los mismos puertos sin cámaras registradas. Servicios Windows (WinSW 2.12.0): `VMSBackend`, `VMSAnalytics`, `VMSHeartbeat`, `VMSCentral`; unidades Linux: `vms`, `vms-analytics`, `vms-heartbeat`, `vms-central`, `vms-central-reports.{service,timer}` (lunes 06:00 Europe/Madrid → `python -m analytics.reports --all-sites --last-week`). La app se importa desde la carpeta de instalación con un `.pth` (venv) o `python312._pth` (embebible); no se instala como paquete. |
| 2026-10-04 | central-deploy | Kiosco: en respuesta a la recomendación (4) de «interfaz web», `deploy/kiosk/start-kiosk.ps1` reabre cada muro cada `-RefreshHours` (6 h por defecto, escalonado 20 s) para renovar la sesión de kiosco antes de `VMS_SESSION_HOURS`. Si la API implanta caducidad deslizante para el kiosco, se puede poner `-RefreshHours 0`. |
| 2026-10-04 | central-deploy | `pyproject.toml`: package-data `central` = `web/*.html`, `web/static/*` (aditivo). `deploy/__init__.py` para `python -m deploy.third_party_notices` (genera `THIRD_PARTY_NOTICES.txt` desde los locks; `tests/central/test_deploy.py` comprueba que está al día). |
| 2026-10-04 | núcleo VMS | §5.1 firmas ampliadas (solo para pruebas, compatibles): `client_for(..., transport=None)` y `test_device(..., transport=None)` aceptan un transporte httpx (mocks ASGI sin red); `discover(..., multicast=None)` (con `targets` y sin `multicast=True` solo sondea esos destinos). ISAPI y CGI son **implementación propia** (no se copió código de terceros, no hace falta `docs/TERCEROS.md` por esta parte). La prueba RTSP es un OPTIONS+DESCRIBE con Digest en Python (sin ffprobe en el producto) y **nunca reintenta una contraseña rechazada**: si la API del equipo da 401, `test_device` no prueba RTSP. |
| 2026-10-04 | núcleo VMS | §4.3 hallazgo verificado con MediaMTX v1.21.1: **MediaMTX vigila `mediamtx.yml` y lo recarga al cambiar; al recargarlo borra todas las rutas añadidas por la API** (`paths: {}`). Por eso el motor escribe el YAML **solo antes de lanzar el proceso** (también en cada relanzamiento, con los ajustes vigentes) y los cambios de grabación/retención en caliente van por `PATCH /v3/config/pathdefaults/patch`. Nadie debe tocar ese archivo con el motor en marcha. |
| 2026-10-04 | núcleo VMS | §5.2 `MediaMtxEngine(settings, paths, *, mediamtx_bin=None, disk_guard_interval=60, watchdog_interval=15, auth_fail_threshold=2, auth_pause_seconds=1800, backoff=(1,2,5,10,30), api_timeout=5)`; extras: `run_disk_guard()`, `paused_devices`, `recordings_dir`, `config_file`. Comportamientos añadidos: (1) **pausa por contraseña rechazada**: tras 2 respuestas 401 de un equipo (mismo host, puerto, usuario y contraseña) se retiran sus rutas para que MediaMTX no reintente cada 5 s y bloquee el usuario; se reintenta con UNA sola petición RTSP cada 30 min o en cuanto cambia la contraseña; `PathStatus.last_error` lo explica. (2) `whep_url`/`rtsp_read_url` usan `main` si la cámara no tiene subflujo. (3) El disk guard **no borra nada** si ni borrando todas las grabaciones antiguas se bajaría del umbral (el disco lo llena otra cosa); lo avisa en `EngineStatus.last_error`. (4) Huérfanos: archivo `<datos>/mediamtx/mediamtx.pid` (el siguiente arranque detiene un MediaMTX nuestro que quedara vivo; probado matando el backend con SIGKILL), `PR_SET_PDEATHSIG` en Linux y Job Object «kill on close» en Windows (este último sin probar). |
| 2026-10-04 | núcleo VMS | §5.3/§6 `vms.api.create_app(settings=None, *, engine=None, credential_store=None, client_factory=None, device_tester=None, discoverer=None, start_engine=True, heartbeat=True, apply_delay=1.0)` y `vms.api.build_state(...)`; `python -m vms` lo implementa el núcleo (`vms/__main__.py`). Si el motor no arranca (binario ausente, puerto ocupado) el backend arranca igual (`/api/health` = `down`) y reintenta cada 30 s. Páginas: rutas de `vms.web.PAGES` + `/wall/{1..4}` con redirección en el servidor (sin sesión → `/login?next=`; kiosco → `/wall/1`; `/setup` solo sin usuarios y desde 127.0.0.1). `/docs` y `/openapi.json` desactivados. Cabeceras `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`. |
| 2026-10-04 | núcleo VMS | §6 añadidos compatibles: `GET /api/status` incluye además `status`, `problems`, `version`, `uptime_s`; `POST /api/devices` y `PATCH` → **409** si ya hay otro equipo con la misma IP y puertos; `POST /api/auth/setup` deja además la sesión abierta; `GET /api/walls/{n}` admite `If-None-Match` → 304; `GET /api/analytics/cameras` devuelve solo las cámaras con ajustes guardados (igual que el backend de pruebas de la interfaz); `details.fields[].loc` sin el prefijo `body` y con enteros para índices (`["cells", 0]`). Peticiones de «interfaz web» atendidas: `details.import_error` dentro de `DeviceOut`; WHEP 404 sin vídeo / 503 motor caído / 502 error del motor; **sesión de kiosco con caducidad deslizante** y cookie de sesión del navegador (sin `Max-Age`). |
| 2026-10-04 | núcleo VMS | §8.8 atendido: `GET /api/internal/analytics/config` usa `analytics.config.build_analytics_config` (una sola implementación de §8.2) quitando antes las cámaras de equipos deshabilitados; `site.id` = `settings.site.id` de config.json (se siembra una vez desde `VMS_SITE_ID`). **ETag por contenido** (hash), no por revisión: la revisión vuelve a 0 al reiniciar el backend y un ETag `"0"` antiguo daría un 304 falso. El snapshot de respaldo usa `analytics.snapshot.grab_jpeg` si está instalado (si no, OpenCV directo). |
| 2026-10-04 | núcleo VMS | Primer administrador: `VMS_ADMIN_INITIAL_PASSWORD` (≥ 8 caracteres) o `/setup` desde el propio equipo, como dice `.env.example`. **No** se genera una contraseña aleatoria para mostrarla en el registro: `vms.log` se guarda en disco y la contraseña quedaría en claro; se registra un aviso con la URL de `/setup`. |
| 2026-10-04 | núcleo VMS | Aviso para «interfaz web» (sin cambiar nada suyo): (1) el Playwright **síncrono** compartido de `tests/web/conftest.py` se abre una vez y se cierra en `atexit`; mientras vive deja un bucle de eventos registrado en el hilo principal y **todas las pruebas asíncronas que pytest ejecuta después fallan** con «Runner.run() cannot be called from a running event loop» (en la ejecución completa, las de `tests/engine`, `tests/tools`, `tests/vendors`). Por separado todo pasa. Solución sugerida: cerrar Playwright al terminar cada módulo o usar la API asíncrona (`tests/api/test_webrtc_browser_e2e.py` lo hace así). (2) `test_ui.py::test_analytics_draw_line_and_zone` es intermitente también con su backend de pruebas: espera un `.toast.ok` que puede ser el de una acción anterior y lee `/api/analytics/cameras` antes de que llegue el PUT. |
| 2026-10-04 | integrador | §3.1: en el `.env` una variable vacía = valor por defecto (`env_ignore_empty`), así que `VMS_MTX_WEBRTC_ICE_TCP=` **no** desactivaba ICE por TCP. Ahora `VmsSettings` acepta `off`/`none`/`disabled`/`-` en `mtx_webrtc_ice_udp` y `mtx_webrtc_ice_tcp` y los convierte en `""` (desactivado). Documentado en `.env.example`. Compatible: un valor explícito `""` por código sigue significando desactivado. |
| 2026-10-04 | integrador | §6.6: el proxy WHEP convierte el error de MediaMTX «path … is not configured» (500 en la ventana entre el alta de una cámara y la aplicación de la configuración al motor) en **404 `stream_not_available`** (y 404 en `OPTIONS`), que es lo que el muro trata como «sin vídeo, reintenta». Antes llegaba al navegador como 502/500. |
| 2026-10-04 | integrador | §8: nueva variable opcional `VMS_ANALYTICS_TELEGRAM_API_BASE` (`AnalyticsSettings.telegram_api_base`, por defecto `https://api.telegram.org`) para un servidor Bot API propio o un proxy; la prueba de sistema la usa para apuntar a un Telegram simulado. |
| 2026-10-04 | integrador | Herramientas (§9, aditivo): `python -m tools.dev_run start [--sim] [--seed] [--pg] [--central] [--detach]` / `status` / `stop` arranca y para todo el sistema de desarrollo (pgserver + simulador con API HTTP simuladas de Hik/Dahua + backend con su MediaMTX + analítica + central) en `.tmp/dev/`; la parada usa un archivo `stop.request` (igual en Windows y POSIX) y comprueba que no quede MediaMTX huérfano. `python -m tests.e2e.system_check` es la prueba de sistema de punta a punta con procesos reales (resultados en `tests/e2e/RESULTADOS.md` y `RESULTADOS-datos.md`). |
| 2026-10-04 | integrador | Pruebas: `tests/web/conftest.py` abre Playwright síncrono **por módulo** (fixture `pw_browser` con `scope="module"`) y lo cierra al acabar; resuelve los 24 fallos + 33 errores «Runner.run() cannot be called from a running event loop» de la batería completa. `test_analytics_draw_line_and_zone` ya no depende de un `.toast.ok` previo (espera al PUT real). `tests/central/ps_check.ps1` con `PositionalBinding = $false`: sin analizador, el primer script se enlazaba a `-AnalyzerPath` y `Import-Module` lo ejecutaba. |
| 2026-10-04 | integrador | §7.2 (compatible): `HeartbeatSender(dsn, site, ...)` acepta también una función `() -> Site`, que se consulta en cada latido. El backend pasa `state.site`: antes pasaba una copia de la sede tomada al arrancar y, si se cambiaba el nombre o la zona horaria desde el panel, el latido directo **sobrescribía** cada 10-60 s el nombre nuevo en `sites` con el antiguo (lo detectó la prueba de sistema: el informe semanal salía como «Sede»). |
| 2026-10-04 | integrador | Prueba con condición de carrera corregida: `tests/api/test_lifespan_misc.py::test_backend_starts_even_if_engine_fails_and_retries` esperaba solo a `engine.running` y comprobaba las rutas antes de que terminara la primera aplicación de la configuración. |
| 2026-10-04 | corrector final | **Revisión de robustez y seguridad** (detalle y pruebas en `docs/ESTADO.md`). Cambios de interfaz, todos compatibles salvo donde se indica: |
| 2026-10-04 | corrector final | Windows: `vms.core.aio` — analítica, informe semanal y panel central arrancan con `SelectorEventLoop` en Windows (psycopg asíncrono no funciona con Proactor); el latido directo del backend usa psycopg **síncrono** en un hilo (`record_heartbeat_sync`), porque el backend necesita Proactor para lanzar MediaMTX. |
| 2026-10-04 | corrector final | §4.3 MediaMTX: sin usuario anónimo (`vms-backend`/`vms-reader`, ver §4.3). La analítica lee el RTSP local con `vms-reader` (`with_reader_credentials`); el proxy WHEP, la línea de tiempo y las descargas usan `vms-backend`. Quien lea MediaMTX directamente necesita esas credenciales. |
| 2026-10-04 | corrector final | §4.3 grabaciones: patrón `…-%f%z`. `disk_guard.parse_segment_name` acepta los dos formatos y, al arrancar, renombra los segmentos antiguos sin desfase con el desfase correcto (fecha + mtime para la hora repetida). |
| 2026-10-04 | corrector final | §5.2 motor: la pausa por contraseña rechazada solo se activa con el código de estado 401 (no con «401» dentro de un puerto como 5401). |
| 2026-10-04 | corrector final | §6.2 sesiones: la cookie de **kiosco** va firmada (HMAC con una clave derivada de `VMS_KIOSK_TOKEN`) y se valida sin estado: sobrevive a reinicios del backend (30 días máx.; cambiar el token la invalida). Login: argon2 en un grupo de 2 hilos y límite adicional de 20 fallos/5 min por IP (cualquier usuario); memoria del limitador acotada. |
| 2026-10-04 | corrector final | §6.3 `PATCH /api/devices/{id}`: si cambia `host`, `http_port`, `rtsp_port` u `onvif_port` y el cuerpo no trae `password`, responde **422** (`details.fields[].loc = ["password"]`): la contraseña guardada no se envía a otra dirección. **Cambio de comportamiento** (antes 200). La interfaz lo pide en el formulario. |
| 2026-10-04 | corrector final | §6.7/§6.4/§6.6: registro de auditoría `logs/audit.log` (una línea JSON por ver/descargar grabación, captura y vista en vivo a resolución completa: usuario, IP, cámara, tramo). §6.9: `PUT /api/settings/retention` con más de 30 días responde 200 con `warning` (art. 22.3 LOPDGDD). §6.10: `ZoneRule` rechaza `clear_below >= alert_threshold` con 422. Páginas HTML con `Content-Security-Policy`. |
| 2026-10-04 | corrector final | §8 analítica: la cola en disco tiene índice en memoria; si el disco está lleno, los lotes se guardan en memoria (con límite) y se intentan escribir directamente en PostgreSQL; el aviso de Telegram sale aunque falle la cola; un minuto ya enviado no se reabre si el reloj retrocede (las muestras tardías van al minuto abierto). `status.json` añade el estado de la cola en disco. |
| 2026-10-04 | corrector final | §7.1: migración `0002_site_row_security` + `python -m vms.db.site_roles create/list/revoke` (rol por tienda con RLS). §7.4 panel central: `X-Forwarded-For` se lee de derecha a izquierda saltando proxies de confianza y hay límite por usuario además de por IP; `?next=` del login solo admite el mismo origen. |
| 2026-10-04 | corrector final | §3.1 nuevas variables `VMS_HTTPS_PORT` (8643), `VMS_TLS_CERT_FILE`, `VMS_TLS_KEY_FILE` y orden `python -m vms tls-cert`: con certificado, HTTPS en `VMS_HTTP_HOST:VMS_HTTPS_PORT` y HTTP solo en `127.0.0.1:VMS_HTTP_PORT` (`vms.api.serve`). Sin certificado, todo igual que antes (con aviso en el registro si escucha fuera de loopback). |
| 2026-10-04 | corrector final | Despliegue: los lock de sede (`requirements-vms/analytics/central.txt`) llevan SHA-256 de PyPI y los instaladores usan `--require-hashes` (comprobado con `pip download --require-hashes` para win_amd64 y manylinux x86_64). Windows: `Protect-Path` sobre toda la carpeta de datos; `models\weights` y `*.pth` no se copian. `THIRD_PARTY_NOTICES.txt` incluye RF-DETR, DINOv2 y COCO. |
| 2026-10-04 | corrector final | §6.12 (encontrado por la prueba de sistema): los formularios de login y primer administrador (VMS y panel central) llegan con el botón desactivado y `method="post"`; el script lo activa al enganchar el envío. Antes, un clic antes de cargar el script enviaba el formulario por GET con la contraseña en la URL. |
| 2026-10-05 | arquitecto | **Versión 2.0 (fase 0 de la v2).** Nuevas §13-§18. Cambios en lo existente, todos compatibles: `CONFIG_VERSION` 2 con migración automática (§13.8); modelos persistentes con `extra="allow"` y cuerpos de petición filtrados con `known_fields_only`; `Vendor` pasa de lista cerrada a identificador validado contra el registro (`DeviceCreate` sigue respondiendo 422 con una marca desconocida); `DeviceKind` admite `dvr` y `xvr`; `DeviceInfo.firmware_date`; extra `[vms]` con `numpy` y `opencv-python-headless` (B6); extra `[test]` y `requirements-test.txt`; `FakeEngine(clock=…)`; rutas de cámaras, vivo y grabaciones llaman a `vms/api/permissions.py` (sin efecto hasta B6); routers vacíos registrados; `devices.js` separado de `panel.js`; contenedores y módulos vacíos en las páginas (§18.9); punto de extensión del panel central (`central/extensions.py`). |
| 2026-10-05 | arquitecto | **Revisión de la fase 0** (antes de lanzar los bloques). Compatibles: §7.3 bis (proveedores del latido por bloque, `vms/core/heartbeat_extras.py`, ya llamado por backend y agente); `VmsSettings.engine_mode` (`VMS_ENGINE_MODE`, `child`) y `update_source` (`VMS_UPDATE_SOURCE`, `https://`, `http://` o `file:///`); §17.3 `events.py` del arquitecto hasta que B2 arranque y eventos v2 congelados; §18.8 ganchos de ámbito también en muros, analítica y `/api/status` (**cambio de comportamiento solo cuando B6 active el ámbito**: un operador con ámbito recibe 422 al añadir una cámara ajena a un muro); §18.12 y §13.1 una sola regla para la tabla de avisos (`data\advisories.json` de la versión desaparece: va dentro de `app\vms\ops\security\`); §13.10 `recordSegmentDuration` medido. Pruebas de concepto: S4 con los nombres de §13.4 (`trial_since_unix`, `updated_unix`), campos desconocidos conservados y espera creciente; S3 con rollback de `snapshot` y clave sustituida rechazada. |

---

# Parte II · v2 (fase 0, 5-oct-2026)

Lo que sigue es el contrato de la v2. Las decisiones y su justificación están en `docs/PLAN-V2.md`;
aquí solo va lo que un bloque necesita para no pisar a otro: rutas, formatos, nombres y comportamientos.
Lo marcado **(pendiente de S1/S5)** se cerrará con el veredicto de esa prueba (`docs/investigacion-v2/spikes.md`).

## 13. Plataforma Windows: disco, servicios, `vmshost`, puntero y diario [B1]

### 13.1 Disposición en disco

```
C:\Program Files\VMSMultimarca\
├── bin\vmshost.exe                    arrancador fijo (solo lo cambia el instalador completo)
├── versions\<X.Y.Z>\                  inmutable; solo escribe el actualizador
│   ├── bin\vmsctl.exe
│   ├── runtime\                       python.exe, python312.dll, python312._pth, Lib\site-packages\ (.pyc)
│   ├── app\                           vms\, analytics\, central\
│   ├── engine\                        mediamtx.exe, MEDIAMTX-LICENSE.txt
│   ├── viewer\VMS.exe
│   ├── models\                        rfdetr-*.xml/.bin, LICENSE
│   ├── THIRD_PARTY_NOTICES.txt
│   └── release.json                   copia del descriptor firmado (§15.4)
└── updater\slot-a\ y slot-b\          vmsctl.exe + runtime mínimo + vms_updater

C:\ProgramData\VMSMultimarca\          (= carpeta de datos, §3.2) +
├── state\active.json, journal.json    escritos SIEMPRE con atomic_write (§13.5)
├── backups\pre-<X.Y.Z>-<AAAAMMDDTHHMMSSZ>\
├── updater\public-status.json         legible por Usuarios (§15.6); cache\; blacklist.json
├── mediamtx\mediamtx.yml              fuente única de rutas (§13.10); ACL estricta
├── secrets\                           DPAPI de máquina + ACL por SID (§13.9)
├── ops\                               datos de B6 (§18.17); ops\advisories\ lo escribe solo VMSUpdater (§18.12)
└── evidence\                          evidencias protegidas y exportaciones (§18.6-§18.7)
```
Sin junctions. Lo que no cambia entre versiones se copia con enlaces duros. `PYTHONDONTWRITEBYTECODE=1`
en todos los servicios.

### 13.2 Servicios y cuentas

| Servicio | Cuenta | Inicio | `ImagePath` |
|---|---|---|---|
| `VMSEngine` | `NT SERVICE\VMSEngine` | automático | `"<bin>\vmshost.exe" service --name VMSEngine` |
| `VMSBackend` | `NT SERVICE\VMSBackend` | automático (retrasado) | ídem con `--name VMSBackend` |
| `VMSAnalytics` | `NT SERVICE\VMSAnalytics` | automático (retrasado) | ídem |
| `VMSHeartbeat` | `NT SERVICE\VMSHeartbeat` | automático (retrasado) | ídem |
| `VMSCentral` | `NT SERVICE\VMSCentral` | automático | ídem |
| `VMSUpdater` | `LocalSystem` | automático (retrasado) | ídem (`vmshost` elige la ranura A/B) |

- ACL siempre por **SID** (`*S-1-5-80-…` del servicio, `*S-1-5-18`, `*S-1-5-32-544`), nunca por nombre
  localizado. El SID de servicio se obtiene con la API (`LookupAccountName("NT SERVICE\<nombre>")`), no
  analizando la salida de `sc showsid` (la prueba S4 lo hace así solo por ser un script).
- Recuperación del SCM: reiniciar a los 1, 5 y 30 s; contador a cero a las 24 h.
- Permisos por carpeta: los de PLAN-V2 §2.2. Grupo local `VMS Operadores` (lectura de `kiosk.token`).

### 13.3 `vmshost.exe`

```
vmshost.exe service --name <Servicio>        (lo lanza el SCM)
vmshost.exe viewer [--walls]                 (accesos directos: abre el visor de la versión activa)
vmshost.exe --version
```
1. Lee `state\active.json`; si falta, está corrupto o apunta a una versión que no existe, lo
   **reconstruye** con `last_good` del diario y lo anota en `logs\vmshost.log`.
2. Lanza `versions\<activa>\bin\vmsctl.exe run --service <nombre>` (o `updater\slot-<x>\vmsctl.exe run
   --service VMSUpdater`) dentro de un **Job Object** con `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. El hijo
   se crea **suspendido** (o con `PROC_THREAD_ATTRIBUTE_JOB_LIST`) para que no haya ventana sin job.
3. Versión «a prueba» (`trial: true`): si el hijo cae **3 veces en 10 min** o pasan **30 min** sin
   confirmación, vuelve a `previous` (puntero escrito con `atomic_write`) y la arranca.
4. Informa al SCM (Running/Stopped) y, al recibir Stop, para el job.
5. **Quién escribe el puntero** (hallazgo de S4): `vmshost` corre con la cuenta del servicio que hospeda, así
   que con el diseño de la prueba todas las cuentas virtuales necesitarían escribir `state\`. En el
   producto **solo** `VMSUpdater` (LocalSystem) y el instalador escriben `active.json`; el `vmshost` de un
   servicio sin privilegios que detecte una versión a prueba fallida escribe `state\rollback-request.json`
   (permiso de crear archivos en esa carpeta, no de modificar el puntero) y para su hijo; el `vmshost` del
   actualizador (o el propio actualizador) ejecuta la vuelta atrás. Decisión de B1, a cerrar en su revisión.

### 13.4 `state\active.json` (puntero)

```json
{"schema": 1,
 "active": "2.1.0", "previous": "2.0.0",
 "trial": true, "trial_since_unix": 1763600000,
 "updater": {"slot": "b", "previous_slot": "a", "trial": false, "trial_since_unix": null},
 "updated_unix": 1763600000}
```
Tiempos en segundos Unix (UTC): sin dependencias de fecha en Rust. Campos desconocidos se conservan.

### 13.5 `state\journal.json` (diario) y `atomic_write`

```json
{"schema": 1, "update_id": "u-20261120T011200Z", "from": "2.0.0", "to": "2.1.0",
 "components": ["app"], "state": "verifying", "attempt": 1, "last_good": "2.0.0",
 "backup": "backups\\pre-2.1.0-20261120T011200Z",
 "steps": [{"state": "downloaded", "started_unix": 1763600000, "done_unix": 1763600050}],
 "error": ""}
```
Estados: `idle → downloaded → backed_up → stopping → switched → migrated → started → verifying → good`
y `→ rolling_back → rolled_back`. Cada paso se anota **antes** y se marca hecho **después**; al arrancar,
`VMSUpdater` retoma o revierte (pasos idempotentes). `last_good` lo leen el actualizador y `vmshost`.

**`atomic_write`** (Python `vms.core.atomic.atomic_write_bytes/_text`, Rust `vms_common::atomic_write`):
temporal `<nombre>.tmp-<pid>` en la misma carpeta → flush + `fsync`/`FlushFileBuffers` → sustitución
atómica (`MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)` en Windows, `rename` + `fsync`
de la carpeta en POSIX), con reintentos si un antivirus bloquea el archivo. Python usa hoy `os.replace`
(sin `WRITE_THROUGH`): **B1 lo cambia** a `MoveFileExW` con `ctypes` en Windows. Se usa para
`active.json`, `journal.json`, `config.json`, `users.json`, `mediamtx.yml` y `public-status.json`.

### 13.6 Ocultación de credenciales

Reglas en `vms.core.rtsp.redact` (Python) y `vms_common::redact` (Rust). Vectores compartidos en
`tests/fixtures/redaction_vectors.json` (`{"version", "secrets", "cases": [{"name", "input", "expected"}]}`),
comprobados por `tests/core/test_redaction_vectors.py` y `cargo test -p vms-common`. Dueño: B1; B5 pide
vectores nuevos cuando una marca tenga un formato de URL distinto. Cambiar una regla = cambiarla en los
dos lenguajes + añadir el vector.

### 13.7 Instalador ↔ actualizador

`DisplayVersion` en `HKLM\…\Uninstall\{AppId}_is1` y `HKLM\SOFTWARE\VMSMultimarca\InstalledVersion` los
escribe el actualizador tras cada actualización o vuelta atrás buenas; el instalador se niega a bajar de
versión salvo `/ALLOWDOWNGRADE`. Cerrojo compartido por la tubería (§15.2, orden `lock`).

### 13.8 Compatibilidad de `config.json` y `users.json`

- `CONFIG_VERSION = 2`. `vms/core/config_migrations.py` (dueño B4): `MIGRATIONS = {1: v1_to_v2, …}`,
  funciones puras sobre el JSON; `ConfigStore` las aplica al cargar.
- Modelos persistentes (`Device`, `Camera`, `WallLayout`, reglas, ajustes, `AppConfig`, `UserPublic`/`User`,
  `Site`): `extra="allow"`. Una versión N-1 conserva lo que no conoce. Nunca se conserva un `password` en
  claro dentro de un equipo (se descarta y se avisa, como en la v1).
- Cuerpos de petición que llegan como `dict` (ajustes, reglas, analítica por cámara): se filtran con
  `vms.core.models.known_fields_only(Modelo, cuerpo)` antes de mezclarse con lo guardado.
- Versión **mayor** que la propia: se carga sin migrar, en **solo lectura** (`ConfigStore.read_only`; el aviso
  de `load()` llega a `config_warning`) y no se guarda nunca: `save()` lanza `NewerConfigError` (409
  `config_read_only`). Tampoco se guarda un `AppConfig` con `version` mayor que `CONFIG_VERSION`.
- `Vendor`: texto `^[a-z0-9][a-z0-9-]{1,31}$`. El alta (`DeviceCreate`, `DeviceUpdate`,
  `DeviceTestRequest`) lo valida contra `known_vendor_ids()` (lo amplía el registro con
  `register_vendor_ids()`); un equipo guardado con una marca desconocida se conserva y la interfaz muestra
  «Driver no disponible en esta versión».

### 13.9 Secretos

DPAPI de máquina (`CryptProtectData` + `CRYPTPROTECT_LOCAL_MACHINE`) + ACL al SID del servicio que lo usa,
SYSTEM y Administradores. Código en `vms/core/winsec.py` (B1, `ctypes`). Archivos en `secrets\`:
`secret.key`, `credentials.enc`, `internal.token`, `kiosk.token` (§17.1), `site.token`, y la clave de
firma de evidencias `evidence-ed25519.key` (§18.7). Migración automática desde el `secret.key` en claro de
la v1. En SSD el borrado seguro no está garantizado (se documenta).

### 13.10 Motor como servicio y YAML como fuente única (resultado de S2)

- `VMS_ENGINE_MODE=child|attach` (por defecto `child` en macOS/Linux de desarrollo; el instalador de Windows
  fija `attach`). `python -m vms engine-config` escribe el YAML; `python -m vms engine-run` hace de `vmsctl`
  en desarrollo.
- En `attach`, el backend escribe `mediamtx\mediamtx.yml` **completo** (rutas con su `source` y
  credenciales codificadas `%XX`) con `atomic_write` en cada cambio; nunca añade rutas por la API.
- Comportamiento verificado con MediaMTX v1.21.1 en S2 (macOS):
  - el renombrado atómico dispara la recarga (0,2 s hasta tener vídeo en una ruta nueva);
  - la recarga **solo reinicia las rutas cuya configuración cambió**: las demás no cortan ni abren segmento;
  - cambiar un ajuste de grabación en `pathDefaults` (`recordDeleteAfter`, `recordSegmentDuration`; medido
    con los dos) no reinicia las rutas, **pero abre un segmento nuevo en todas** (hueco medido ≤ 0,8 s con
    GOP de 1 s, es decir, ≤ 1 GOP). `record: false` en una sola ruta no afecta a las demás. Por eso la retención se cambia como un ajuste excepcional (se avisa en la
    interfaz) y `recordDeleteAfter` se mantiene en el YAML: así la retención funciona aunque el backend
    esté caído días;
  - motor matado (SIGKILL) y relanzado con el mismo YAML → vuelve a grabar todo en 0,2 s sin backend;
  - un 401 de la cámara no escribe la contraseña en el registro (la redacción de `vmsctl` sigue siendo
    obligatoria como defensa); la API de MediaMTX **sí** devuelve el `source` con la contraseña: la API
    sigue solo en 127.0.0.1 con usuarios internos (§4.3).
- Pausa por 401: se quitan del YAML las rutas del equipo afectado; `vmsctl` escribe `logs\engine.log` ya
  redactado y el backend lo sigue con `vms/engine/logtail.py` (tolera la rotación).
- Los usuarios internos de MediaMTX no admiten `:`, `/` ni `%` en la contraseña (hallazgo de S2): las de la
  v1 se derivan en hexadecimal, así que no cambia nada.

## 14. `vmsctl` [B1]

### 14.1 Órdenes

```
vmsctl run --service <Servicio>                       anfitrión del proceso real (lo lanza vmshost)
vmsctl services install --role control|store|central|viewer --data-dir <ruta>
vmsctl services uninstall [--purge]
vmsctl services start|stop|restart [--only VMSBackend,VMSEngine]
vmsctl firewall apply --profiles private[,domain] | firewall remove
vmsctl acl apply --data-dir <ruta>
vmsctl ports check
vmsctl health wait --timeout 120 [--deep]
vmsctl version switch <X.Y.Z> | version show
vmsctl update check|status|rollback [--to X.Y.Z] [--reason <texto>]   (por la tubería; exige elevación)
vmsctl tls setup --hostname <nombre> [--import-root]
vmsctl kiosk rotate
vmsctl diag bundle --out <zip>                        registros y estado, nunca secretos ni mediamtx.yml
vmsctl migrate-from-v1                                servicios WinSW → vmshost, datos intactos
```
Todas aceptan `--json`. `run`: lanza `runtime\python.exe -m vms` (o `engine\mediamtx.exe`…), lo mete en
su Job Object, parada en 3 escalones (`POST /api/internal/shutdown` o `CTRL_BREAK` → 10 s →
`TerminateJobObject`), reinicio con backoff 1/2/5/10/30 s y `logs\<servicio>.log` redactado con rotación
10 × 10 MB.

### 14.2 Códigos de salida y JSON

| Código | Significado |
|---|---|
| 0 | ok |
| 2 | uso incorrecto |
| 10 | puerto ocupado |
| 11 | sin permisos |
| 12 | health check fallido |
| 20 | error de Windows (código Win32 en `error.win32`) |

Salida con `--json` (una sola línea, UTF-8):
`{"ok": false, "code": 10, "data": {…}, "error": {"code": "port_in_use", "message_es": "El puerto 8600 está ocupado por …", "win32": null}}`.
Nunca se analiza texto localizado de Windows. Constantes en `vms_common::exit_codes`.

## 15. Actualizador [B4]

### 15.1 Fuentes de actualización

`VMS_UPDATE_SOURCE` (lo fija el instalador): `https://updates.<dominio>/` (Worker, §15.5),
`http://<host>:<puerto>/` (repositorio estático: pruebas y CI) o `file:///D:/vms-updates` (USB o carpeta
compartida, repositorio `offline`). La verificación TUF es idéntica en las tres. El `root` de confianza
viene en el instalador (`updater\trusted\online\1.root.json` o `offline\1.root.json`).
**Decisión de la noche del 5/10:** sin Cloudflare todavía; el Worker se prueba con Miniflare y la prueba
real de actualización usa un servidor HTTP local en CI.

### 15.2 Tubería de control `\\.\pipe\VMSMultimarca.updater`

ACL: SYSTEM y Administradores (token elevado). Mensajes JSON de una línea, petición → respuesta:

| Petición | Respuesta |
|---|---|
| `{"cmd": "status"}` | contenido de `public-status.json` + `journal` |
| `{"cmd": "check"}` | `{"ok": true, "found": "2.1.0" \| null}` |
| `{"cmd": "rollback", "to": "2.0.0" \| null, "reason": "…"}` | `{"ok": true, "update_id": "…"}` o error |
| `{"cmd": "hold", "on": true \| false}` | `{"ok": true}` |
| `{"cmd": "lock", "owner": "installer", "ttl_s": 3600}` / `{"cmd": "unlock", "owner": "installer"}` | `{"ok": true}` o `{"ok": false, "error": "busy"}` |

### 15.3 Estados y fallos inyectados

Estados del diario: §13.5. Variables de prueba (solo en builds de prueba): `VMS_UPDATER_FAULT_AT=<estado>`
(mata el proceso justo antes y después del paso) y `VMS_UPDATER_PAUSE_AT=<estado>` (espera 30 s y lo
anuncia en `public-status.json`).

### 15.4 Repositorio, descriptor y canales

Formato de PLAN-V2 §2.7 sin cambios (TUF con `consistent_snapshot`, `bundles/`, `components/<c>/`,
`channels/`, `installers/`, y además `data/advisories-<AAAAMMDD>.json` como componente `data`, sin reinicio:
`VMSUpdater` lo verifica, valida el esquema y lo escribe con `atomic_write` en
`<datos>\ops\advisories\advisories.json`; regla de cuál manda en §18.12). `root` y `targets`: **ECDSA P-256** (S3: verificado con python-tuf 7.0.1 + securesystemslib 1.5.1;
`HSMSigner` usa python-pkcs11, MIT). `snapshot`/`timestamp`: ed25519.
**Claves de desarrollo:** se generan fuera del repositorio (`%USERPROFILE%\.vms-dev-keys\` o el token SoftHSM
`vms-dev`), sus archivos y etiquetas empiezan por `dev-` y el `root` de desarrollo lleva
`"x-vms-env": "dev"` en `signed`; un cliente de producción solo confía en el `root` de producción que trae
el instalador, así que una firma de desarrollo nunca vale en una tienda. La ceremonia real (2 YubiKey +
papel) queda documentada en `docs/PUBLICAR-VERSION.md` (B4) y se hará cuando se compren las llaves (D4).

### 15.5 Worker (`infra/update-worker/`)

`GET /metadata/<archivo>` libre (caché corta). `GET /targets/<ruta>` con `Authorization: Bearer <token>`:
KV `site_tokens`, clave `<cliente>:<sha256 hex del token>`, valor `{"site": "S0042", "active": true}`.
Sin token o revocado → 401 `{"error":"unauthorized"}`; token de otro cliente → 403; descarga registrada
(cliente, sede, versión). Cabecera `Date` siempre presente (comprobación de reloj, §15.6).

### 15.6 `updater\public-status.json` y latido

```json
{"schema": 1, "installed": "2.1.0", "channel": "stable", "state": "good", "hold": false,
 "last_check": "2026-11-20T03:10:00Z", "last_result": "update_ok|update_failed|metadata_expired|clock_skew|none",
 "message_es": "", "available": null, "metadata_expires": "2026-11-27T00:00:00Z", "clock_skew_s": 0.4,
 "reboot_pending": false, "updated": "2026-11-20T03:10:05Z"}
```
Lo leen el visor (diagnóstico) y `/status` (`GET /api/updates/status`, router `updates`, rol O). El latido
añade `payload.update` con esos mismos campos (aditivo a §7.3). La respuesta del latido HTTP puede traer
`{"update": {"check": true, "channel": "pilot", "hold": false, "rollback_to": null}}`.

### 15.7 Panel central

Tabla `site_versions` (migración `0003_site_updates.sql`) y rutas en `central/updates.py` (registradas por
`central/extensions.py`): `GET /api/updates/sites`, `PUT /api/updates/sites/{id}` (`channel`, `hold`,
`window`), `POST /api/updates/sites/{id}/rollback` (A). El panel **no firma** nada.

## 16. Registro de drivers [B5]

### 16.1 Tipos (código: `vms/core/interfaces.py`)

`DriverSpec` (id, name, brands, kinds, capabilities, default_ports, auth, maturity, detect, lockout,
presets, client, notes_es, setup_hints_es), `Capability`, `StreamPreset`, `LockoutPolicy`,
`DetectionHints`, `DriverPublic`. Registro: `vms/vendors/registry.py` (`REGISTRY`, `get_driver`,
`best_match`), un archivo por driver en `vms/vendors/drivers/<id>.py`, que llama a
`register_vendor_ids()`. `client_for`, `test_device` y `build_camera_sources` consultan el registro; sus
firmas (§5.1) no cambian.

### 16.2 Capacidades

`api_probe`, `api_channels`, `api_snapshot`, `api_codec_fix`, `onvif`, `onvif_media2`, `discovery_wsd`,
`discovery_sadp`, `discovery_dhip`, **`time_read`** (implementa `DeviceClockClient.device_time() ->
DeviceTime`; lo usa B6) y **`security_read`** (implementa `DeviceSecurityClient.security_settings(admin_username,
admin_password) -> DeviceSecuritySettings`; lo usa B6 con credenciales temporales que no se guardan ni se
registran); `rtsps` y `mjpeg_http` (v2.1); `events` y `ptz` (fuera de la v2).

### 16.3 Madurez e identidad

`maturity`: `verified` (fila de 72 h en `docs/COMPATIBILIDAD.md`, lo comprueba
`tests/vendors/test_maturity_matches_matrix.py`), `fixtures`, `community`, `experimental`.
`Device.identity: DeviceIdentity | None` (`serial`, `mac`, `source`, `seen_at`), `Device.allow_basic`
(por defecto `false`) y `Device.follow_ip` (por defecto `false`).

### 16.4 `GET /api/vendors` (rol A)

Respuesta: `[DriverPublic]` (sin funciones). La interfaz construye el formulario de alta en
`#device-form-root` desde `vms/web/static/js/devices.js` (dueño B5) y elimina `presetPaths` de JS.

## 17. Visor de escritorio [B2] (pendiente de S1 y S5)

### 17.1 Entrada de los muros sin escribir contraseñas

`ProgramData\VMSMultimarca\secrets\kiosk.token` (ACL: SYSTEM, Administradores y el grupo local
`VMS Operadores`). El visor lo lee y lo cambia por la cookie de kiosco con
`POST /api/local/kiosk-session` (`{"token": "…", "next": "/wall/1"}` → 204 + cookie; solo desde
127.0.0.1; router `local`), sin pasarlo por la línea de órdenes ni por la URL. `vmsctl kiosk rotate` lo
cambia. Un usuario fuera del grupo ve «Sin permiso para abrir los muros».

### 17.2 Ventanas y datos

`%APPDATA%\VMSMultimarca\viewer.json`:
`{"schema": 1, "servers": [{"name", "url", "sha256"}], "walls": [{"wall": 1, "server": "local", "monitor_key": "<nombre>|<x>,<y>|<ancho>x<alto>"}]}`.
Todas las ventanas comparten la carpeta de datos de WebView2. Páginas remotas sin IPC (capacidades de
Tauri solo para `tauri://localhost`). Fijación del certificado del servidor remoto: **pendiente de S5**.

### 17.3 Eventos SSE nuevos (`vms/api/events.py`: dueño el arquitecto hasta que B2 arranque; después B2)

`engine` (`{"state": "started|restarted|down", "pid", "at"}`, lo emite B1: el muro pone a cero su espera)
y `update` (`{"version", "viewer_restart", "state", "message_es"}`, lo emite B4). Además B6 emite
`health`, `bookmark`, `evidence` y `notice` (§18.18). Nombres y cargas declarados en código
(`V2_EVENTS`, los `TypedDict` y las funciones `publish_engine`, `publish_update`, `publish_health`,
`publish_bookmark`, `publish_evidence` y `publish_notice`). Están **congelados**: emitirlos no exige tocar el
archivo, y un evento o un campo nuevo se pide por §12 (lo aplica el arquitecto mientras B2 espera a S1).

## 18. Operación, IA de verificación y onboarding [B6]

### 18.1 Principios

- **RGPD:** ni reconocimiento facial, ni identificación, ni búsqueda de personas, ni métricas de
  trabajadores, ni audio. La salud de imagen mide **la cámara**, no a la gente. La única imagen que se
  guarda es la **referencia** de cada cámara (mediana de unos 15 fotogramas tomados durante unos minutos,
  que borra a los transeúntes) y la del último aviso de sabotaje; se guardan en `ops\references\` con ACL
  de datos y nunca viajan en avisos, en el latido ni en el informe. Tratamiento mínimo documentado en
  `docs/RGPD-EIPD.md` («Salud de cámara»).
- **Licencias (lo que se distribuye):** OpenCV clásico + NumPy (ya en `[vms]`), `cryptography` (Ed25519),
  biblioteca estándar (`sqlite3`, `smtplib`), `httpx`; **Driver.js 1.9.0 (MIT)** copiado en
  `vms/web/vendor/driver.js/` con su `LICENSE` (sin CDN). Descartados por licencia o por RGPD: Shepherd.js
  e Intro.js (AGPL), pyiqa (no comercial), SuperPoint, DINOv3, cve-search (AGPL), nmap/python-nmap,
  smartmontools, py-SMART, libx264, CLIP sobre imágenes, ANPR. DINOv2/LightGlue/ALIKED solo si el método
  clásico falla en el piloto (no en la v2).
- Los textos de causa, avisos y diagnóstico son en lenguaje de tienda, con qué hacer, y el estado nunca se
  indica solo con color (icono + texto).
- Código en `vms/ops/` (paquete de B6), rutas en los routers vacíos ya registrados (§18.9) y modelos base
  en `vms/ops/models.py`. Los ajustes guardados en `config.json` están en `vms/core/models.py`
  (`HealthSettings`, `NotificationSettings`, `CameraHealthConfig`, `CameraScope`): B6 pide cambios a B5.

### 18.2 Salud de imagen (prioridad 1)

- **Dónde corre:** en el backend (también en puestos sin analítica), tarea en segundo plano con
  `asyncio.to_thread`, una comprobación por cámara cada `settings.health.check_interval_s` (180 s por
  defecto) repartidas en el intervalo. Imagen: subflujo, por `DeviceClient.snapshot` o, si falla, un
  fotograma del RTSP local de MediaMTX (como `/api/cameras/{id}/snapshot`). Nunca se decodifica de forma
  continua. Objetivo: < 20 ms de CPU por comprobación a 640 px.
- **Referencias:** día y noche (IR). «Fijar referencia» toma ~15 fotogramas en ~3 min y guarda la mediana;
  queda en la auditoría. Se avisa si la mediana aún muestra a alguien quieto («fíjala con la tienda
  cerrada»).
- **Medidas y causas** (`HealthMetrics`, `HealthCause`): negra (luma < ~10 y % < 8 alto), tapada (desviación
  y entropía bajas, pocos bordes), desenfocada (`var(Laplaciano)` relativa y bordes conservados), movida o
  girada (ORB + `estimateAffinePartial2D` con **puerta de inliers ≥ 15 %**; si no, la causa es otra),
  mira a otro sitio, congelada (diferencia ≈ 0 con el OSD tapado), IR atascado/débil, degradada, contraluz,
  dominante de color, ruido. Zonas excluidas: `Camera.health.masks`.
- **Puntuación 0-100:** 100 − penalizaciones; tapada/negra/congelada = 0; movida −40 a −60; desenfoque
  −10 a −40; color/ruido/contraluz −5 a −15. Estado: ≥ 80 `ok`, 50-79 `warning`, < 50 `critical`; sin
  referencia o sin imagen: `unknown`. **Histéresis:** `settings.health.hysteresis` comprobaciones seguidas
  antes de cambiar de estado.
- **Rutas** (router `health`):

| Método y ruta | Rol | Petición → Respuesta |
|---|---|---|
| `GET /api/camera-health` | O | → `[CameraHealth]` (respeta el ámbito por cámara) |
| `GET /api/camera-health/{camera_id}` | O | → `CameraHealth` |
| `POST /api/camera-health/{camera_id}/reference` | A | `{"kind": "day" \| "night"}` → 202 `{"job_id"}`; al terminar, evento `health` |
| `GET /api/camera-health/{camera_id}/reference.jpg?kind=day` | A | → `image/jpeg`, `Cache-Control: no-store`; queda en `audit.log` |
| `GET /api/camera-health/{camera_id}/now.jpg` | A | → imagen actual en memoria (no se guarda) para «Antes / Ahora» |
| `PUT /api/camera-health/{camera_id}/config` | A | `CameraHealthConfig` → `CameraHealthConfig` |
| `POST /api/camera-health/{camera_id}/check` | A | → `HealthCheck` (comprobación inmediata) |

### 18.3 Hora y desfase (prioridad 3)

Lectura por `DeviceClockClient.device_time()` (B5; ONVIF `GetSystemDateAndTime` sin autenticación, ISAPI
`/ISAPI/System/time`, Dahua `getCurrentTime`) y hora del PC (en Windows, estado de `w32tm` por API, sin
analizar texto). Desfase = hora del equipo − hora del PC a mitad del viaje. Umbrales
`settings.health.clock_warn_s` (2 s) y `clock_critical_s` (30 s); también aviso si `time_mode` es manual.
Cada medida se guarda (§18.17) y la exportación incluye la última en el manifiesto.
Rutas: `GET /api/clock` (O) → `{"pc": ClockCheck, "devices": [ClockCheck]}`; `POST /api/clock/check` (A).

### 18.4 Previsión de días de grabación (prioridad 4)

Tasa real por cámara (bytes/hora en `recordings\` de las últimas 24 h) frente a disco libre + lo que la
retención borrará. `GET /api/retention-forecast` (O) → `RetentionForecast`;
`POST /api/retention-forecast/simulate` (A) `{"add_cameras": 4, "bitrate_mbps": 4.0}` → `RetentionForecast`.
Aviso si los días previstos < `settings.retention.days`; aviso RGPD si el objetivo > 30 días.

### 18.5 Informe de salud (prioridad 2)

`GET /api/health-report?date=AAAA-MM-DD` (O) → `HealthReport` (día en la zona de la sede);
`GET /api/health-report.csv?date=` (O). Contenido: disponibilidad por cámara, huecos de grabación (minutos
sin segmento, desde los spans del motor), fps y tasa frente a lo esperado, puntuación de imagen mínima,
desfase, días de retención reales, tramos protegidos, disco (libre, previsión y, en Windows, estado SMART
leído con `Get-PhysicalDisk`/`Get-StorageReliabilityCounter` invocados desde `vmsctl diag`, nunca con
smartmontools). Latido (aditivo a §7.3): `payload.health = {"report_date", "status", "score_min",
"cameras_critical", "cameras_warning", "clock_worst_s", "forecast_days", "problems": ["…"]}` (máx. 10
frases, sin imágenes ni IP).

### 18.6 Marcadores y bloqueo de retención (prioridad 6)

`Bookmark`/`BookmarkCreate` (`vms/ops/models.py`), id `bm-xxxxxxxx`. Rutas (router `evidence`):
`GET /api/bookmarks?camera_id=&from=&to=` (O), `POST /api/bookmarks` (O con permiso `bookmark`; `protect`
exige A o permiso `export`), `PATCH /api/bookmarks/{id}` y `DELETE /api/bookmarks/{id}` (A; se audita).
«Proteger» crea **enlaces duros** (copia si no es el mismo volumen) de los segmentos del tramo en
`evidence\protected\<camera_id>\` con su `meta.json` (motivo, caso, caducidad, usuario): la retención de
MediaMTX borra el original pero el enlace sobrevive. Caducidad por defecto 90 días; al caducar, B6 borra la
copia y lo anota. El disk guard cuenta `evidence\` y nunca la borra. Evento SSE `bookmark`.

### 18.7 Exportación de evidencias (prioridad 5)

Rutas: `POST /api/evidence/exports` (O con permiso `export`) `EvidenceExportRequest` → 202 `EvidenceExport`;
`GET /api/evidence/exports/{id}`; `GET /api/evidence/exports/{id}/download` (ZIP en streaming);
`DELETE /api/evidence/exports/{id}` (A). Progreso por SSE `evidence`. Motivo obligatorio. Línea en
`audit.log` (`{"event": "evidence_export", "user", "ip", "cameras", "from", "to", "reason", "case_ref", "export_id", "sha256_manifest"}`).

**Paquete** (`<export_id>.zip`, `export_id` = `ev-AAAAMMDD-xxxxxx`):
```
LEEME.txt                      cómo verificar, en español
visor.html                     autocontenido (JS/CSS en línea, sin red): lista de clips, <video>, marca de
                               agua superpuesta (usuario y fecha; NUNCA quemada en el vídeo) y comprobación
                               de los SHA-256 con crypto.subtle; avisa si algo no coincide
manifiesto.json                EvidenceManifest (schema 1): sede, intervalo UTC y local, desfase medido,
                               usuario, motivo, caso, receptor y SHA-256 + tamaño de CADA archivo
manifiesto.sig                 firma Ed25519 (base64) de los bytes exactos de manifiesto.json
clave-publica.pem              clave pública de la instalación (key_id = SHA-256 de la clave en bruto)
acta.html                      acta de cadena de custodia (quién exporta, quién recibe, huellas, códec y
                               recomendación de VLC para H.265)
video/<camera_id>/segments/…   segmentos fMP4 ORIGINALES de MediaMTX (el «nativo»)
video/<camera_id>/<cámara>_<AAAAMMDD-HHMMSS>.mp4   unión sin recodificar (remux)
```
Clave de firma: `secrets\evidence-ed25519.key` (se crea en el primer uso; DPAPI de máquina cuando B1
entregue `winsec`); la clave pública viaja en el latido (`payload.evidence_key = {"key_id", "public_key"}`)
para que la central la tenga. Verificación: `python -m vms.ops.evidence verify <zip>` (sale con 0 si todo
cuadra). Nunca se recodifica (sin libx264); H.265 se entrega tal cual.

### 18.8 Permisos por cámara (prioridad 12)

`User.camera_scope: CameraScope | None` (`cameras`, `live`, `playback`, `export`, `bookmark`). `None` =
todas (compatibilidad v1); los administradores no tienen ámbito; el kiosco ve el vivo de las cámaras de
los muros. Se aplica **solo** en `vms/api/permissions.py`, ya llamado desde la fase 0 en:
- cámaras, vivo, grabaciones y descargas: una cámara fuera del ámbito responde 404;
- `PUT /api/walls/{monitor}`: una cámara que se **añade** al muro tiene que estar en el ámbito `live` (si
  no, 422 «La cámara no existe», como una inexistente); las que ya estaban se pueden dejar o quitar. Así un
  operador con ámbito no puede ver una cámara ajena a través del kiosco;
- analítica: `GET /api/analytics/rules` y `/analytics/cameras` filtran, `GET /api/analytics/rules/{id}` de
  una cámara ajena responde 404 y `GET /api/analytics/status` solo trae sus cámaras (`live`);
- `GET /api/status`: `cameras` y `analytics.cameras` filtradas (`live`). El usuario del agente de sede
  (`VMS_AGENT_USERNAME`) tiene que ser un operador **sin** ámbito, o el latido saldría incompleto.
`GET /api/walls` no se filtra (el kiosco necesita los identificadores; no lleva nombres). Lo administra `PATCH /api/users/{username}` con
`camera_scope` (router `users`, dueño B6 en la v2). Pendiente de pedir a B2: filtrar el evento SSE
`status` por ámbito.

### 18.9 Contenedores, módulos y estilos en las páginas

Ya están en las páginas (vacíos y ocultos); el dueño solo edita su módulo:

| Página | Contenedor | Módulo JS | Dueño |
|---|---|---|---|
| `index.html` | `#onboarding-root`, `#help-root` | `onboarding.js`, `help.js` | B6 |
| `index.html` (diálogo de alta) | `#device-form-root` | `devices.js` | B5 |
| `status.html` | `#updates-root` | `updates.js` (+ `updates.css`) | B4 |
| `status.html` | `#health-root`, `#security-audit-root`, `#notifications-root` | `health.js`, `security.js`, `notifications.js` | B6 |
| `playback.html` | `#timeline-events-root`, `#bookmarks-root`, `#evidence-root` | `timeline-events.js`, `bookmarks.js`, `evidence.js` | B6 |
| `analytics.html` | `#counts-export-root` | `counts-export.js` | B6 |

Hoja `ops.css` (B6) en esas páginas. `wall.html` no tiene nada de B6: **nunca** recorridos ni avisos en
los muros en kiosco.

### 18.10 Avisos por correo y webhook (prioridad 7)

`settings.notifications: NotificationSettings` (en `config.json`, sin secretos). La contraseña SMTP y el
secreto del webhook van al `CredentialStore` con las claves `notify:smtp_password` y `notify:webhook_secret`.
Reglas (`NotificationRule`): tipos, gravedad mínima, canales, horas de silencio y **agrupación** («5
cámaras caídas en la tienda 37» en vez de 5 mensajes). Por defecto, **sin imagen**.
Webhook: `POST` JSON con cabecera `X-VMS-Signature: sha256=<HMAC-SHA256 hex del cuerpo con el secreto>`:
```json
{"schema": 1, "site": {"id": "s0037", "name": "Tienda 37", "code": "037"}, "at": "2026-11-20T10:00:00Z",
 "severity": "critical", "kind": "tamper", "title_es": "Cámara tapada: Cajas 2", "count": 1,
 "cameras": [{"camera_id": "cam-1a2b3c4d", "name": "Cajas 2"}], "details": {"score": 0, "causes": ["covered"]}}
```
Rutas (router `notifications`): `GET/PUT /api/notifications/settings` (A; nunca devuelve secretos, solo
`has_smtp_password`), `PUT /api/notifications/secrets` (A), `POST /api/notifications/test` (A,
`{"channel": "email"|"webhook"}`), `GET /api/notifications/log` (A) → `[NotificationRecord]`. Telegram
sigue como en la v1 (§8.5).

### 18.11 «¿Por qué no conecta?» (prioridad 9)

`POST /api/diagnostics/device` (A) `{"device_id"}` o un `DeviceTestRequest` → `DiagnosisResult`;
`POST /api/diagnostics/camera/{camera_id}` (A). Reglas deterministas en orden (`DiagnosisStep.code`):
`ping` → `tcp_http` → `tcp_rtsp` → `http_response` → `auth` → `lockout` → `rtsp_describe` → `codec` →
`clock` → `engine` → `path_ready`. **Un solo intento con credenciales** (respeta `LockoutPolicy`). Cada paso
trae la causa probable y la acción en lenguaje claro. El LLM es opcional (`VMS_LLM_*`) y solo reescribe
`summary_es` a partir de los pasos, **sin IP, usuarios ni contraseñas** (`llm_used`).

### 18.12 Auditoría de seguridad de equipos (prioridad 11)

Solo equipos dados de alta, sin explotar nada ni probar contraseñas. `POST /api/security-audit/run` (A)
`{"admin_credentials": {"<device_id>": {"username", "password"}}}` (opcional, no se guardan ni se registran)
→ `SecurityAuditReport`; `GET /api/security-audit/latest` (A); `GET /api/security-audit/advisories` (A)
→ versión y fecha de la tabla. Comprobaciones (`SecurityFinding.check`): contraseña guardada débil o de
fábrica (análisis **local**), usuario `admin` en vez del de solo lectura, RTSP/ONVIF anónimos (una petición
sin credenciales), Telnet/SSH/HTTP sin TLS/puertos SDK/UPnP/P2P (TCP con tiempo límite y, con credenciales
de administrador, `security_read`), firmware con CVE y hora. Resultado: «Vulnerable (KEV)», «Probablemente
vulnerable», «Sin CVE conocidos en la tabla» o «Desconocido». **Nunca «seguro».**

**Tabla de avisos propia** (`AdvisoryTable`, `schema` 1). Hay **dos copias** y una sola regla:
- **la de la versión**: `vms/ops/security/advisories.json` (en disco, `versions\<X>\app\vms\ops\security\`;
  la mantiene B6). Es la base y la que vale si no hay otra;
- **la que llega entre versiones**: componente TUF `data` (§15.4, PLAN-V2 §2.7) que `VMSUpdater` escribe con
  `atomic_write` en `<datos>\ops\advisories\advisories.json` (nadie más la escribe);
- el backend (B6, `vms/ops/security/`) lee las dos y usa la **válida** (esquema `AdvisoryTable` 1) con el
  **`generated_at` más reciente**; con el mismo `generated_at`, la de la versión. Una copia inválida se
  ignora con un aviso en el registro. `GET /api/security-audit/advisories` dice cuál usa
  (`"origin": "version" | "update"`, `generated_at`, `kev_catalog_version`). La prepara Unmanned en el PC de
publicación (NVD API 2.0 + catálogo KEV de CISA, CC0 + EPSS de FIRST), así **ni las tiendas ni la central
salen a Internet** y la tabla va firmada como cualquier otra parte del producto.
```json
{"schema": 1, "generated_at": "2026-10-05T00:00:00Z", "source": "Unmanned Studio", "kev_catalog_version": "2026.10.04",
 "nvd_notice": "This product uses the NVD API but is not endorsed or certified by the NVD.",
 "advisories": [{"id": "ADV-2026-001", "vendor": "hikvision", "model_regex": "^DS-2CD", "families": ["IPC_G3"],
   "compare": "build_date", "fixed_build_date": "2021-06-28", "fixed_version": null,
   "cves": ["CVE-2021-36260"], "kev": true, "kev_date_added": "2022-01-10", "cvss": 9.8, "epss": null,
   "vendor_advisory_url": "https://www.hikvision.com/…", "reviewed": "2026-10-05", "notes_es": "…"}]}
```
Hikvision se compara por fecha de build (NVD no trae rangos); Dahua por versión por partes. Revisión
trimestral; para empezar, los CVE de Hikvision y Dahua del KEV y de gravedad alta.

### 18.13 Línea de tiempo con eventos (prioridad 13)

`GET /api/timeline/{camera_id}?start=&end=&layers=recording_gap,bookmark,health,clock,analytics_alert,protected`
(O, permiso `playback`) → `[TimelineEvent]`. Las capas se pintan sobre la línea de tiempo propia de
`playback.html` (sin vis-timeline mientras baste). «Sin grabación» se pinta distinto de «hay grabación».
Eventos del NVR (`nvr_event`): v2.1.

### 18.14 Onboarding (prioridad 10)

`GET /api/onboarding/state` y `PUT /api/onboarding/state` (O) → `OnboardingState` (por usuario, en
`ops\onboarding.json`). Asistente de primer uso cuando no hay equipos y el usuario es administrador (pasos de
la investigación §5.2: bienvenida, buscar, credenciales, probar con el diagnóstico, canales, referencia de
imagen, muro, grabación con previsión, listo). Ayuda «?» con textos en `vms/web/static/help/es.json`
(preparado para catalán e inglés). Estados vacíos con las 3 pautas (estado, enseñar, camino directo).
Recorridos con Driver.js solo para administrador o técnico.

### 18.15 Exportación CSV de conteos (prioridad 8)

`GET /api/analytics/counts.csv?from=&to=&bucket=hour|day` (O; router `counts`; necesita `VMS_PG_DSN`, si no
503 `not_configured`). Columnas `tienda;fecha;hora;entradas;salidas;cola_media;cola_max`, separador `;`,
coma decimal, UTF-8 **con BOM**, fechas en la zona de la sede. Solo agregados.

### 18.16 Panel central

Rutas en `central/ops.py` (registradas por `central/extensions.py`): `GET /api/ops/problems?date=` («tiendas
con problemas hoy», ordenadas por gravedad, a partir de `payload.health` del latido),
`GET /api/ops/sites/{site_id}/health?date=`, `GET /api/ops/problems.csv`, `GET /api/sites/{site_id}/counts.csv`
y `GET /api/counts.csv?sites=…` (mismas reglas que §18.15). El PDF es la página imprimible del navegador
(sin bibliotecas de PDF).

### 18.17 Persistencia de B6

`<datos>\ops\ops.sqlite3` (SQLite de la biblioteca estándar, modo WAL; un solo escritor: el backend):
tablas `health_checks` (90 días), `clock_checks` (400 días), `bookmarks`, `evidence_exports`,
`notifications_log` (90 días) y `timeline_events`. Referencias de imagen en `ops\references\<camera_id>-day.jpg`
y `-night.jpg`. Nada de esto entra en `diag bundle` salvo los recuentos.

### 18.18 Eventos SSE de B6

Declarados en `vms/api/events.py`: `health` (`HealthEvent`), `bookmark` (`BookmarkEvent`), `evidence`
(`EvidenceEvent`) y `notice` (`NoticeEvent`; la interfaz lo muestra en el panel, **nunca** en los muros).

### 18.19 Criterios de «terminado» de B6

Los de PLAN-V2 §6.2 (B6). Imprescindibles: imágenes sintéticas reproducibles para cada causa (como la
prueba de la investigación §3.2) con la puntuación y la causa esperadas; paquete de evidencias que
`verify` acepta y que falla si se toca un byte; webhook firmado verificado por un servidor simulado;
`test_spanish_style.py` verde; ninguna imagen de personas guardada (prueba que recorre `ops\` tras una
ejecución).
