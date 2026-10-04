# Panel central multi-tienda

El panel central muestra, para todas las tiendas:

- **Estado**: en línea, con avisos (cámaras sin vídeo, disco lleno, analítica parada), caída
  (sin latido en 3 intervalos) o sin datos.
- **Conteos de hoy y de la semana** por tienda (entradas y salidas, en la zona horaria de cada
  tienda) y la variación respecto al mismo tramo de la semana anterior.
- **Comparativa entre sedes** (hoy, esta semana, 7 o 30 días).
- **Colas con más alertas** (número, tiempo total en alerta, pico de personas).
- **Detalle por tienda**: afluencia por hora o por día, ocupación media de la cola, alertas,
  cámaras y reglas de analítica.
- **Informes semanales** de cada tienda.

Solo lee PostgreSQL y recibe latidos: no tiene acceso a cámaras ni a vídeo.

## Instalación (servidor Linux)

1. PostgreSQL 16 en el servidor (paquete del sistema). Crea la base y los roles:
   ```sql
   CREATE DATABASE vms;
   CREATE ROLE vms_central LOGIN PASSWORD '...';
   ```
   Las tiendas **no** comparten un rol: cada una tiene el suyo (paso 3).
2. Aplica el esquema: `python -m vms.db.migrate --dsn postgresql://postgres@localhost/vms`
   (o `python -m central migrate` con `VMS_PG_DSN` definido).
3. Permisos mínimos:
   ```sql
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO vms_central;
   GRANT INSERT, UPDATE ON sites, site_cameras, site_heartbeats, weekly_reports TO vms_central;
   ```
   (`vms_central` necesita escribir `sites`, `site_cameras` y `site_heartbeats` porque recibe los
   latidos HTTP, y `weekly_reports` para el informe semanal.)

   **Un rol por tienda.** La migración `0002_site_row_security` activa *Row Level Security*: el rol
   de una tienda solo puede leer, insertar y modificar las filas de su `site_id` (no puede borrar
   nada ni ver otras sedes). Así, si un PC de tienda queda comprometido, no puede tocar los conteos,
   las alertas ni los nombres de las otras 146. Créalo en el servidor con un usuario que pueda
   crear roles:
   ```bash
   python -m vms.db.site_roles create site-bcn-001 --dsn postgresql://postgres@localhost/vms
   ```
   Genera una contraseña aleatoria, da los permisos mínimos y muestra **una vez** la línea
   `VMS_PG_DSN=…` para el `.env` de esa tienda (revisa el host: debe ser la IP del servidor en la
   VPN). `list` muestra los roles creados y `revoke site-bcn-001` borra el de una tienda. Los roles
   que no son de tienda (`vms_central`, el administrador) siguen viendo todas las sedes.
4. Instala el panel: `sudo deploy/linux/install.sh --components central` y completa en
   `/etc/vms-multimarca/vms.env`:
   ```
   VMS_CENTRAL_PG_DSN=postgresql://vms_central:CLAVE@127.0.0.1:5432/vms
   VMS_CENTRAL_ADMIN_INITIAL_PASSWORD=una-clave-larga      # crea «admin» en el primer arranque
   VMS_PG_DSN=postgresql://vms_central:CLAVE@127.0.0.1:5432/vms   # para el informe semanal
   VMS_LLM_PROVIDER=anthropic        # o «none» para informes solo con cifras
   VMS_LLM_MODEL=...
   VMS_LLM_API_KEY=...
   ```
   Después: `sudo systemctl start vms-central vms-central-reports.timer`. Borra
   `VMS_CENTRAL_ADMIN_INITIAL_PASSWORD` del archivo tras el primer arranque.
5. Abre `http://IP-VPN-DEL-SERVIDOR:8700/`. Si no definiste contraseña inicial, crea el primer
   administrador en `/setup` **desde el propio servidor** (por ejemplo con un túnel SSH:
   `ssh -L 8700:127.0.0.1:8700 servidor` y abre `http://127.0.0.1:8700/setup`).

**HTTPS:** pon delante un proxy inverso con TLS (Caddy o nginx) escuchando en la IP de la VPN, y
fija `VMS_CENTRAL_SECURE_COOKIES=true` y `VMS_CENTRAL_TRUSTED_PROXIES=["127.0.0.1"]`. Dentro de la
VPN el tráfico ya va cifrado por WireGuard, pero HTTPS evita avisos del navegador y protege si
alguien accede desde fuera de la VPN por error.

## Cómo informan las tiendas (latido)

Hay dos formas; las dos escriben lo mismo (`sites`, `site_cameras`, `site_heartbeats`):

| | Latido directo | Agente HTTP |
|---|---|---|
| Quién lo envía | el propio servicio VMS de la tienda | servicio aparte `python -m central.agent` |
| Cómo se activa | `VMS_PG_DSN` en el `.env` de la tienda | `VMS_CENTRAL_URL` + `VMS_SITE_TOKEN` |
| Necesita | credenciales de PostgreSQL en la tienda | solo un token de la tienda |
| Úsalo si | la tienda tiene analítica (ya escribe en PostgreSQL) | la tienda no debe tener acceso a la base |

### Token por tienda (agente HTTP)

En el panel: **Administración → Tokens de latido por sede** → escribe el identificador de la
tienda (`site-bcn-001`, el mismo que `VMS_SITE_ID`) → **Crear o rotar token**. Copia el token
(solo se muestra una vez) en el `.env` de la tienda:
```
VMS_SITE_ID=site-bcn-001
VMS_CENTRAL_URL=https://central.vpn:8700
VMS_SITE_TOKEN=vms_...
# opcional pero recomendado: usuario operador del VMS para enviar el estado de cada cámara
VMS_AGENT_USERNAME=latido
VMS_AGENT_PASSWORD=...
```
También por consola en el servidor: `python -m central token create site-bcn-001`
(`token list`, `token revoke`). En el servidor solo se guarda el SHA-256 del token.

El agente envía cada `VMS_HEARTBEAT_SECONDS` (60 s): versión, estado, cámaras con vídeo y
grabando, disco, analítica y temperatura de CPU (en Linux). Si la central no responde reintenta
a los 2, 5 y 10 s y, si sigue sin responder, lo deja para el siguiente ciclo (no acumula). Si el
backend de la tienda no responde, envía igualmente un latido con estado «caída» para que se vea
que el PC está encendido pero el vídeo no. Prueba manual: `python -m central.agent --once` (o
`--dry-run` para ver el latido sin enviarlo).

## API

Todas las respuestas en JSON con fechas en UTC (`Z`). Las peticiones que modifican exigen la
cabecera `X-Requested-With: vms` (protección CSRF) y la sesión del panel (cookie
`vms_central_session`, `HttpOnly`, `SameSite=Strict`). Errores: `{"error": {"code", "message",
"details"}}`. Roles: O = operador o administrador, A = administrador.

| Método y ruta | Rol | Descripción |
|---|---|---|
| `GET /api/health` | — | estado del panel y de la base de datos |
| `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` | —/O | sesión (5 fallos en 5 min por IP y usuario → 429) |
| `GET/POST /api/auth/setup` | — | primer administrador, solo desde el propio servidor |
| `GET /api/sites` | O | todas las sedes con estado, último latido, conteos de hoy/semana, alertas |
| `GET /api/sites/{id}` | O | una sede, con cámaras y reglas |
| `GET /api/sites/{id}/counts?range=&from=&to=&bucket=hour\|day&rule_id=` | O | entradas/salidas por tramo, en la zona horaria de la sede (tramos vacíos = 0) |
| `GET /api/sites/{id}/occupancy?…` | O | ocupación de la cola por tramo (media, máximo, segundos por encima del umbral) |
| `GET /api/sites/{id}/queue-alerts?range=&from=&to=&limit=` | O | alertas de cola con duración |
| `GET /api/sites/{id}/reports` · `/reports/{AAAA-MM-DD}` | O | informes semanales |
| `GET /api/compare?range=&from=&to=&tz=` | O | comparativa entre sedes activas |
| `GET /api/queues/top?range=&from=&to=&limit=&tz=` | O | colas con más alertas |
| `GET /api/reports/latest` | O | último informe de cada sede |
| `GET /api/site-tokens` · `POST/DELETE /api/site-tokens/{site_id}` | A | tokens de latido |
| `GET/POST /api/users` · `PATCH/DELETE /api/users/{usuario}` | A | usuarios del panel (siempre queda un administrador) |
| `POST /api/heartbeat` | token | latido del agente (`Authorization: Bearer <token>`) |

`range` admite `today`, `yesterday`, `week` (desde el lunes), `prev_week`, `last7` y `last30`;
`from`/`to` en ISO 8601 lo sustituyen. Una sede pasa a «caída» si no envía latido en
`VMS_CENTRAL_DOWN_AFTER_INTERVALS` (3) veces su intervalo.

## Ajustes del panel (`VMS_CENTRAL_*`)

| Variable | Defecto | Uso |
|---|---|---|
| `VMS_CENTRAL_PG_DSN` (o `VMS_PG_DSN`) | — | base de datos |
| `VMS_CENTRAL_HTTP_HOST` / `_HTTP_PORT` | `0.0.0.0` / `8700` | escucha |
| `VMS_CENTRAL_DATA_DIR` | `<datos>/central` | usuarios, tokens, registros |
| `VMS_CENTRAL_SESSION_HOURS` | 12 | caducidad de la sesión |
| `VMS_CENTRAL_SECURE_COOKIES` | false | cookie solo por HTTPS |
| `VMS_CENTRAL_ADMIN_INITIAL_PASSWORD` | — | crea `admin` si no hay usuarios |
| `VMS_CENTRAL_HEARTBEAT_SECONDS` (o `VMS_HEARTBEAT_SECONDS`) | 60 | intervalo esperado si el latido no lo indica |
| `VMS_CENTRAL_DOWN_AFTER_INTERVALS` | 3 | latidos perdidos para dar una sede por caída |
| `VMS_CENTRAL_TRUSTED_PROXIES` | `[]` | proxies inversos de confianza (`X-Forwarded-For`) |

## Informe semanal

Lo genera `python -m analytics.reports --all-sites --last-week` los lunes a las 06:00 (hora de
Madrid): en Linux con `vms-central-reports.timer`; en Windows con la tarea programada «VMS
Multimarca - Informe semanal». Las cifras se calculan en SQL y son la fuente de verdad; el
**proveedor LLM configurable** solo redacta el texto a partir de ellas (si está desactivado o
falla, el informe se guarda solo con cifras). Al proveedor solo viajan agregados anónimos.
