# Publicar una versión de VMS Multimarca

> Guía de publicación de la v2 (B4). Referencias: PLAN-V2 §1.5-§1.7 y §2.5-§2.8, CONTRATO §15.
> Todo lo que se ejecuta aquí se ejecuta en el **PC de publicación** de Unmanned Studio, nunca en una tienda ni
> en el panel central de un cliente. Las órdenes son `python -m tools.release …` desde la raíz del repositorio.

## 0. Piezas y quién firma qué

| Pieza | Para qué | Dónde vive | Quién la usa |
|---|---|---|---|
| `root` (3 claves ECDSA P-256, umbral **2 de 3**) | Decide qué claves valen para todo lo demás | YubiKey A, YubiKey B y una copia en papel en caja fuerte | Ceremonias (rotaciones), 1 vez al año |
| `targets` (ECDSA P-256) | **Autoriza cada paquete, canal y tabla de avisos** | YubiKey A (otra ranura PIV) | Cada publicación, en local, con PIN y toque |
| `snapshot` + `timestamp` (ed25519) | Frescura de los metadatos (7 días el `timestamp`) | Secreto de GitHub (`publish-meta.yml`, `timestamp.yml`) | CI, a diario |
| `offline-timestamp` (ed25519) | `snapshot`/`timestamp` del repositorio de espejos USB (60 días) | PC de publicación, cifrada | Al preparar un USB |
| Authenticode (Certum OV, decisión D1) | SmartScreen, antivirus y segunda capa del actualizador | Certum (pendiente: decisión N1) | Al publicar, con `jsign` |
| Token de API de Cloudflare | Alta y baja de tokens de sede en el KV del Worker | PC de publicación | `site-token` |

**Por qué así:** si roban la cuenta de GitHub, el atacante tiene `snapshot`/`timestamp` pero **no puede autorizar ni
un paquete**: eso lo hace `targets`, que está en la llave física. Lo peor que puede hacer es dejar de refrescar el
`timestamp` (las tiendas dejan de actualizar y lo avisan con `metadata_expired`).

**Hoy (noche del 5/10, decisiones N1-N3):** no hay certificado, ni YubiKey, ni cuenta de Cloudflare. Se trabaja
con **claves de desarrollo** (software o SoftHSM), un servidor HTTP estático y Miniflare. Todo está preparado para
cambiar a lo real sin tocar el código (secciones 2.2 y 9).

## 1. Preparar el PC de publicación (una vez)

```bash
python3.12 -m venv .venv && .venv/bin/pip install --require-hashes --no-deps -r requirements-dev.txt
.venv/bin/pip install --no-deps -e .
export VMS_RELEASE_KEYS=~/.vms-release/keys       # llavero (nunca dentro del repositorio)
export VMS_RELEASE_REPO=~/.vms-release/repo       # repositorios TUF online y offline
```

Con YubiKey además: el módulo PKCS#11 `ykcs11` (de Yubico) y `export PYKCS11LIB=/ruta/a/libykcs11.so|.dylib|.dll`.
El PIN se pide por consola (o `VMS_RELEASE_PIN` en una sesión que no deje rastro).

## 2. Llaves

### 2.1 Desarrollo (hoy)

```bash
python -m tools.release keys init-dev                    # claves software en ~/.vms-dev-keys (o $VMS_RELEASE_KEYS)
python -m tools.release keys init-dev --pkcs11-lib /usr/lib/softhsm/libsofthsm2.so --pin 1234   # con SoftHSM
python -m tools.release keys show
python -m tools.release init                             # crea online/ y offline/ con su 1.root.json
```

- Los archivos y etiquetas se llaman `dev-*` y el `root` lleva `"x-vms-env": "dev"`. El instalador de producción
  solo confía en el `root` de producción, así que **una firma de desarrollo nunca vale en una tienda**.
- El actualizador acepta versiones sin Authenticode solo si su `root` de confianza es de desarrollo.
- El llavero y los repositorios no se mezclan: `tools.release` se niega a firmar un repositorio de producción con
  un llavero de desarrollo y al revés.

### 2.2 Ceremonia real con YubiKey (cuando se compren: decisión D4)

**No verificado con hardware todavía** (S3 se hizo con SoftHSM; falta repetirla con las YubiKey). Materiales: 2
YubiKey 5, un PC sin red (arrancado desde un USB de solo lectura), una impresora local, sobre para la caja fuerte y
una segunda persona que custodie la YubiKey B (sin ella, el umbral protege contra perder o que roben una llave, no
contra quien guarda las dos).

1. **Sin red.** En el PC aislado, con `ykman` (yubikey-manager): cambiar PIN, PUK y clave de gestión de las dos llaves.
2. **Generar dentro de cada llave** (la clave privada nunca sale):
   - YubiKey A, ranura PIV 9c: `ykman piv keys generate --algorithm ECCP256 9c rootA.pub` y su certificado
     autofirmado (`ykman piv certificates generate --subject "CN=vms root A" 9c rootA.pub`), para que `ykcs11` la vea.
   - YubiKey A, ranura 9d: la de `targets`, igual.
   - YubiKey B, ranura 9c: la segunda de `root`.
3. **Tercera clave de `root` en papel:** se genera en el PC aislado (`python -m tools.release keys …` con clave
   software), se cifra con una frase de paso larga, se imprime como QR, se comprueba que se puede leer y se borra el
   archivo. El papel va a la caja fuerte; la frase de paso, en otro sitio.
4. **Llavero de producción** (`keyring.json`, `"env": "prod"`): `root` = `hsm:` de A y B + la de papel; `targets` =
   `hsm:` de A; `snapshot`/`timestamp` = `envpem:VMS_TUF_SNAPSHOT_PEM` / `envpem:VMS_TUF_TIMESTAMP_PEM` (secretos de
   GitHub); `offline-timestamp` = archivo cifrado del PC de publicación.
5. `python -m tools.release init` con ese llavero: firma `1.root.json` de `online` y de `offline` con 2 de 3.
6. Copiar los dos `1.root.json` al instalador (B3: `updater\trusted\online\` y `offline\`). **Ese archivo es la raíz
   de confianza de todas las tiendas.**
7. Levantar acta (fecha, asistentes, huellas de las claves, dónde queda cada llave) y guardarla con el papel.
8. Rotación anual del `root` (caduca a 1 año): misma ceremonia con `root-rotate` (sección 10).

## 3. Publicar una versión

1. **Artefactos.** `build.yml` deja los zips por componente (`components/<c>/<c>-<versión>.zip`), el SBOM y la
   atestación, sin firmar. Bajarlos y comprobarlos: `gh attestation verify <zip> --owner <organización>`.
   Los zips llevan `MANIFEST.sha256` y son reproducibles (`python -m tools.release package --component app --src …
   --out …` con `SOURCE_DATE_EPOCH`): dos builds limpias dan el mismo SHA-256.
2. **Firma Authenticode** de los PE (cuando haya certificado): `python -m tools.build sign` (B3, `jsign` con sello
   de tiempo RFC 3161). Sin certificado (N1) se publica sin firmar y sin `--authenticode-o`.
3. **Ensayo:** `python -m tools.release publish --version 2.1.0 --artifacts dist/ --channel pilot --dry-run`.
   Trabaja sobre una copia temporal, la valida con el cliente real (`ngclient`, partiendo de `1.root.json` como un
   equipo recién instalado) y no cambia nada. Comprueba que sale `"ok": true` en `online` y `offline`.
4. **Publicar:**
   ```bash
   python -m tools.release publish --version 2.1.0 --artifacts dist/ --channel pilot \
       --notes-es "Corrige la autenticación Digest SHA-256 con cámaras Hikvision recientes." \
       [--security --severity critical] [--min-from 2.0.0] \
       [--windows-build-min 22631] [--webview2-min 120.0.0.0] \
       [--authenticode-o "<razón social>" --authenticode-issuer "<CA de Certum>"]
   ```
   - **Windows mínimo:** por defecto el descriptor pide el build **19045** (Windows 10 22H2, admitido con ESU por
     la decisión D11), así que Windows 10 y todos los Windows 11 reciben la versión, también los arreglos de
     seguridad. Sube el mínimo con `--windows-build-min` **solo** si la versión de verdad no funciona en builds
     anteriores (esas tiendas verán `error` con el build que hace falta); `--windows-build-min 0` = sin mínimo.
   - Los componentes que no vienen en `dist/` se heredan de la versión publicada inmediatamente anterior por
     número (mismo hash: las tiendas no los descargan). Solo se reinician los servicios de los componentes que cambian (`engine` = hueco ≤ 15 s, solo en
     la ventana; `app`/`runtime` = 0 s de grabación).
   - Si hay una migración de la central marcada `-- reversible: no`, hace falta `--accept-irreversible` (el rollback
     de la central restaura el `pg_dump`).
   - `security` + `severity critical` sin `engine`: se aplica fuera de la ventana.
   - En desarrollo firma también `snapshot`/`timestamp` (`--meta local`); en producción (`--meta ci`) los firma
     `publish-meta.yml` con el secreto de CI.
5. **Subir:** `python -m tools.release upload --to <carpeta del servidor estático>` (orden: paquetes, metadatos y
   `timestamp.json` el último). A R2, cuando exista la cuenta (D5): misma estructura bajo `online/`.
6. **Comprobar:** `python -m tools.release verify`.

## 4. Canales y despliegue por tandas

- `pilot` (3 tiendas de Covert, D10) → `stable` (resto), aprobado por Unmanned + Covert (D9).
- Mover el canal: `python -m tools.release channel stable --version 2.1.0` (firma `targets`).
- **Por tandas:** en el panel central, página **Versiones** (`/updates`), cambiar a `pilot` las sedes de la tanda;
  las demás siguen en `stable`. El panel no elige versiones: solo canal entre canales firmados, retener, ventana,
  «Comprobar ahora» y «Volver a la anterior».
- **Pausar** un canal (nadie nuevo recibe la versión): `channel stable --pause`; reanudar: `--resume`.

## 5. Revertir una versión publicada

En orden de preferencia:
1. **Pausar el canal** (minutos): ninguna tienda más la instala.
2. **Volver atrás en las sedes afectadas:** panel central → «Volver a la anterior» (llega con el siguiente latido),
   o en la tienda con permisos de administrador: `vmsctl update rollback` (elevado; la bandeja del visor lo ofrece).
   Se restaura la configuración del respaldo previo y se avisa de que los cambios hechos después se pierden.
   - **La versión de la que se vuelve queda «omitida» en esa tienda:** el ciclo siguiente NO la reinstala aunque
     el canal siga apuntando a ella (sin esto, la vuelta atrás se desharía sola a las pocas horas). Se ve en
     `/status` («Omitida tras volver atrás») y en **Versiones** («Omitida tras volver atrás: 2.1.0»).
   - La omisión se levanta sola en cuanto el canal ofrece una versión **mayor** (p. ej. la 2.1.1 con el arreglo).
   - Para volver a instalar esa misma versión (p. ej. el problema era de la tienda, no de la versión): botón
     **«Permitir de nuevo la 2.1.0»** en **Versiones**, o en la tienda, elevado,
     `python -m vms_updater unskip [--version 2.1.0]` (con B1, `vmsctl update unskip`). Se instala en la
     siguiente comprobación dentro de la ventana.
   - Si la vuelta atrás la hizo la propia tienda porque la versión falló (health check, firma, cortes repetidos),
     la versión queda como **fallida**, no omitida: «Permitir de nuevo» no la levanta; solo una versión mayor.
3. **Mover el canal a la versión anterior:** `channel stable --version 2.0.0`. Las tiendas **no bajan solas** (solo
   por rollback), pero las que no se hayan actualizado dejan de recibir la mala.
4. **Lo normal: publicar 2.1.1 con el arreglo.** Una versión que falló en una tienda queda en su lista negra local y
   no se reintenta, pero una mayor sí. Se puede publicar el arreglo para `stable` aunque haya una versión mayor en
   `pilot` (p. ej. la 2.1.1 con la 2.2.0 en `pilot`): hereda los componentes de la 2.1.0. Lo que no se permite es
   que un canal retroceda al publicar (la 2.1.1 no puede ir a `pilot` si `pilot` está en la 2.2.0).

Si el health check (120 s) falla en una tienda, el actualizador vuelve solo a la anterior, la pone en la lista negra
y lo informa (`update_failed`) en el latido, en `/status` y en la página **Versiones**.

## 6. Tabla de avisos de seguridad (componente `data`)

```bash
python -m tools.release data advisories advisories-20261005.json
```
Valida el esquema 1 (`AdvisoryTable`), la publica como `data/advisories-AAAAMMDD.json` firmada por `targets` y las
tiendas la instalan en `<datos>\ops\advisories\advisories.json` en su siguiente comprobación (sin reiniciar nada).
El backend usa la más reciente entre esta y la que trae la versión (CONTRATO §18.12).

## 7. Preparar un USB (tiendas sin Internet)

```bash
python -m tools.release mirror --channel stable --out E:\        # la versión que tenga ese canal (por defecto stable)
python -m tools.release mirror --version 2.1.0 --out E:\         # comprueba que algún canal esté en la 2.1.0
```
- La tienda instala **lo que diga su canal**, no una versión elegida a mano. Por eso el USB lleva solo los canales
  que apuntan a su versión; si pides `--version` y ningún canal (o el canal pedido) está en ella, se rechaza en el
  momento con un mensaje claro en vez de dejar un USB inútil. Una tienda de otro canal que use ese USB verá
  «El espejo USB no trae … prepara un USB nuevo con `--channel <canal>`».
- Copia del repositorio `offline` la cadena de `root`, `targets`, el descriptor, sus componentes, los canales que
  apuntan a esa versión y la última tabla de avisos, y firma un `snapshot`/`timestamp` nuevos que **caducan a los 60 días**. Lo valida con el
  cliente real leyendo el USB como `file://`.
- En la tienda, el instalador en modo «sin Internet» fija `VMS_UPDATE_SOURCE=file:///E:/` (o la carpeta compartida)
  y confía en el `root` de `offline`. La verificación es idéntica a la de Internet.
- Pasados 60 días, la tienda **no aplica nada** y avisa: «prepara un USB nuevo». Nunca se ignora la caducidad, ni con
  el reloj mal.

## 8. Tokens de sede (Worker)

```bash
python -m tools.release site-token add --client covert --site S0042 --out secrets.json
python -m tools.release site-token revoke --client covert --site S0042
python -m tools.release site-token revoke-client --client covert      # da de baja a todo un cliente
python -m tools.release site-token list --client covert
```
- El token (`covert.<43 caracteres>`) se muestra **una vez** (o va directo al `secrets.json` que el instalador lee
  con `/SECRETS=` y borra). En el KV solo queda su SHA-256.
- Necesita `CF_ACCOUNT_ID`, `CF_KV_NAMESPACE_ID` y `CF_API_TOKEN` (token de API con permiso solo sobre ese KV).
  Sin cuenta todavía: `--kv-file kv.json` (mismo formato; lo usan las pruebas con Miniflare).
- La tienda guarda el token en `secrets\update.token` (cifrado con DPAPI de máquina, B1). **No** es el token del
  latido del panel central.

## 9. Servidor de actualizaciones (Cloudflare R2 + Worker, decisión D5)

Pendiente de la cuenta. Cuando exista:
1. Bucket R2 `vms-updates` (privado) con `online/metadata/`, `online/targets/` y `offline/`.
2. Espacio KV `site_tokens` y, opcional, Analytics Engine `vms_update_downloads`.
3. Rellenar los identificadores en `infra/update-worker/wrangler.toml` y `npx wrangler deploy` desde
   `infra/update-worker/` (la ruta `updates.<dominio>/*`).
4. Fuente de las tiendas: `VMS_UPDATE_SOURCE=https://updates.<dominio>/<cliente>/`.
5. Probar con `python -m tools.release verify` y con una tienda del laboratorio antes del piloto.

Mientras tanto, el Worker se prueba con Miniflare (`cd infra/update-worker && npm ci && npm test`) y la prueba de
punta a punta (`tests/updater/test_worker_miniflare.py`) actualiza un equipo simulado a través de él.

## 10. Si roban una llave

| Qué se compromete | Qué hacer |
|---|---|
| **`targets`** (YubiKey A robada con su PIN, o el PC de publicación) | 1. Con **2 de las 3 llaves `root`** (B + papel), publicar un `root` nuevo que revoca la clave y añade la nueva: `python -m tools.release root-rotate --role targets --revoke <clave> [--add-soft …]` (en producción, la clave nueva se genera en una YubiKey nueva y se añade al llavero como `hsm:`). 2. Volver a firmar `targets` con la nueva (lo hace la orden) y publicar. 3. Las tiendas siguen la cadena de `root` y **rechazan lo firmado con la clave vieja**, aunque el atacante tenga también `snapshot`/`timestamp`. 4. Revisar qué se publicó desde la fecha del robo. |
| **Una llave `root`** (YubiKey perdida) | `root-rotate --role root --revoke <clave> --add-soft …` con las otras dos (el `root` nuevo lo firma el umbral del anterior sin la clave que sale y el umbral del nuevo). Regenerar la llave en una YubiKey nueva en ceremonia. |
| **Dos llaves `root`** | No hay forma segura de rotar: hay que **reinstalar** el `root` de confianza en las tiendas (instalador nuevo). Por eso la B la guarda otra persona y el papel está en la caja fuerte. |
| **`snapshot`/`timestamp`** (secreto de GitHub o la cuenta) | Rotar el secreto y `root-rotate --role snapshot|timestamp …`. Mientras tanto el atacante solo puede congelar o retrasar metadatos, y eso caduca a los 7 días. Revisar el acceso a GitHub (2FA con llave física, D2). |
| **`offline-timestamp`** | `root-rotate --role timestamp` en el repositorio `offline` y preparar USB nuevos. |
| **Certificado Authenticode** | Revocarlo con Certum y publicar una versión firmada con el nuevo. El actualizador comprueba sujeto y CA, no la huella: acepta el nuevo y los rollback a versiones firmadas con el anterior siguen valiendo (sello de tiempo). |
| **Token de sede** | `site-token revoke` (401 desde ese momento) y uno nuevo en la tienda. Un cliente entero: `revoke-client`. |
| **Token de API de Cloudflare** | Revocarlo en el panel de Cloudflare y crear otro. No puede publicar versiones (el contenido lo protege TUF). |

**Ensayo en CI:** `tests/updater/test_key_compromise.py` (clave `targets` robada: lo firmado con ella se rechaza y lo
nuevo se acepta; rotación de una llave `root` con 2 de 3; negativa si quedan menos de 2 llaves válidas).

**Ensayo de verdad, 5-oct-2026** (claves de desarrollo, carpeta temporal, salida real resumida):

```
$ python -m tools.release keys init-dev                       → código 0
$ python -m tools.release init                                → código 0
$ python -m tools.release publish --version 2.0.0 --artifacts … --channel stable   → código 0
# la clave de targets se da por robada
$ python -m tools.release root-rotate --role targets --revoke dev-targets --add-soft dev-targets-2   → código 0
  {"root_versions": {"online": 2, "offline": 2}, "revoked": ["dev-targets"], "added": ["dev-targets-2"]}
$ python -m tools.release publish --version 2.1.0 --artifacts … --channel stable   → código 0
  {"version": "2.1.0", "new_components": ["app"], "inherited": ["runtime"], "targets_version": 4, …}
$ python -m tools.release verify                              → código 0
  online: root v2, root/targets ecdsa-sha2-nistp256, snapshot/timestamp ed25519, versiones 2.0.0 y 2.1.0
  offline: root v2, igual
# una llave root perdida
$ python -m tools.release root-rotate --role root --revoke dev-root-2 --add-soft dev-root-4   → código 0
  {"root_versions": {"online": 3, "offline": 3}, …}
$ python -m tools.release verify                              → código 0   (el cliente parte de 1.root.json y sigue 1→2→3)
```
Pendiente: repetirlo con las YubiKey reales y una vez al año (PLAN-V2 §1.6).

## 11. Qué ve cada tienda y problemas frecuentes

`/status` (operador), la bandeja del visor y la página **Versiones** del panel muestran `public-status.json`:

| Resultado | Qué significa | Qué hacer |
|---|---|---|
| `update_ok` / `no_update` | Al día | — |
| `waiting_window` | Descargada; se aplica en la ventana (01:00-03:00 por defecto) | — |
| `reboot_pending` | Windows tiene un reinicio pendiente: espera a la siguiente ventana | Reiniciar el equipo fuera de horario |
| `held` | Retenida desde el panel | Quitar «Retenida» cuando toque |
| `update_failed` | Falló y volvió sola a la anterior (o se interrumpió y se reintentará) | Ver el mensaje; publicar una corrección |
| `rollback_ok` | Vuelta atrás pedida hecha; la versión de la que se volvió queda omitida (§5) | «Permitir de nuevo» si procede |
| `metadata_expired` | Metadatos caducados: servidor sin refrescar (`timestamp.yml`) o USB de más de 60 días | Revisar CI o preparar un USB nuevo |
| `clock_skew` | El reloj del equipo difiere más de 5 min del servidor (también con días de desfase, aunque los metadatos parezcan caducados): no se aplica nada | Corregir la hora (NTP) |
| `min_from` | La versión exige una instalada más nueva | Instalador completo |
| `disk_full` | Sin espacio: 2× la versión + 1 GB en la instalación, la versión en la caché de `<datos>` y el componente mayor en la carpeta temporal | Liberar disco |
| `error` | Sin conexión, token rechazado (401/403) o metadatos que no son de confianza | Ver el mensaje |

## 12. Referencia rápida

```
python -m tools.release keys init-dev | keys show
python -m tools.release init
python -m tools.release package --component <c> --src <carpeta> --out <zip>
python -m tools.release publish --version X.Y.Z --artifacts <dist> [--channel c] [--dry-run] [--meta local|ci]
                                [--windows-build-min N|0] [--webview2-min V]
python -m tools.release channel <c> (--version X.Y.Z | --pause | --resume)
python -m tools.release data advisories <tabla.json>
python -m tools.release mirror [--channel stable | --version X.Y.Z] --out <USB> [--days 60]
python -m tools.release verify [--mode online|offline] [--dir <carpeta> --file]
python -m tools.release sign-meta [--mode online] [--timestamp-only]       (CI)
python -m tools.release root-rotate --role <rol> --revoke <clave> [--add-soft <nombre>]
python -m tools.release upload --to <carpeta>
python -m tools.release site-token add|revoke|revoke-client|list --client <c> [--site S] [--kv-file F] [--out secrets.json]
```
En la tienda (consola elevada): `python -m vms_updater status|check|rollback|unskip` o, con B1, `vmsctl update …`.

## Publicar en GitHub (página de descarga)

Mientras no haya certificado ni claves de producción, el instalador se publica en **GitHub Releases** para que se
pueda descargar sin compilar nada:

1. Escribe las notas en `docs/versiones/vX.Y.Z.md` (copia `docs/versiones/PLANTILLA.md`; el nombre es la etiqueta
   exacta, con la «v»). Súbelas a `v2`.
2. Con el CI en verde en ese commit, crea y sube la etiqueta: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. `build.yml` compila el instalador de producto (binarios y visor reales, sin parámetros de prueba ni roots de
   desarrollo), genera el SBOM y crea la versión con el instalador, `SHA256SUMS.txt` y el SBOM. Una etiqueta con
   guion (`v2.0.0-beta.1`) sale como *pre-release*; sin guion, como la última versión.

El botón «Descargar» del README apunta siempre a la última versión publicada.
