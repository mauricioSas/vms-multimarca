# Informe: sistema de actualizaciones

> Informe de investigación para la v2 (5 de octubre de 2026). Se conserva tal como se entregó al
> arquitecto. Las decisiones finales están en [`../PLAN-V2.md`](../PLAN-V2.md).

**Arquitectura recomendada del actualizador (VMS Multimarca, Windows, 1-150 equipos)**

**Recomendación:** un actualizador propio y pequeño, hecho con **python-tuf** (cliente `ngclient`) y que corre como un **servicio de Windows aparte**. Ningún framework del mercado actualiza servicios con migración, health check y rollback. Los que se acercan son de apps de escritorio (Velopack, WinSparkle, Tauri) o están abandonados (Omaha, PyUpdater).

## 1. Frameworks (verificado con la API de GitHub el 05/10/2026)

| Proyecto | Licencia | Estado | Encaje |
|---|---|---|---|
| [Velopack](https://github.com/velopack/velopack) | MIT | Activo, 1.2.161 (29/09/2026); SDK Python `velopack` 1.2.161 MIT | Deltas con zstd y canales. El despliegue escalonado solo existe en su servicio Flow ([doc](https://github.com/velopack/velopack.docs/blob/master/docs/distributing/Flow/tiered-rollout.mdx)). Instala por usuario en `%LocalAppData%`. Para todo el equipo hay que usar MSI, y el issue abierto [#1040](https://github.com/velopack/velopack/issues/1040) dice que en ese modo los hooks corren sin elevar: malo para servicios. Las actualizaciones se comprueban con hash SHA. No encontré una firma propia del feed: **no verificado**. |
| [Tauri updater](https://v2.tauri.app/plugin/updater/) | Apache-2.0/MIT | 2.x estable, v3.0.0-alpha.4 | Exige firma ("cannot be disabled"). Ejecuta el instalador NSIS/MSI. No gestiona servicios. Sirve si la ventana de la app se hace con Tauri. |
| [WinSparkle](https://github.com/vslavik/winsparkle) | MIT | v0.9.4 (21/07/2026) | Firma Ed25519 (EdDSA) en el appcast. Solo descarga y lanza un instalador: sin servicios, sin rollback y sin umbral de firmas. |
| [electron-builder/updater](https://github.com/electron-userland/electron-builder) | MIT | 26.17.0 | En Windows comprueba el editor Authenticode, sin firma propia (**no verificado**). Además obliga a usar Electron. |
| [Google Omaha](https://github.com/google/omaha) | Apache-2.0 | **Archivado**, último release en 2023 | Descartado. |
| [PyUpdater](https://github.com/Digital-Sapphire/PyUpdater) | — | **Archivado** en 2022 | Descartado. |
| [python-tuf](https://github.com/theupdateframework/python-tuf) | Apache-2.0 | v7.0.1 (01/09/2026), activo | **Base recomendada**: rotación de claves, umbral de firmas y protección ante congelación (freeze) y vuelta atrás (rollback). |
| [tufup](https://github.com/dennisvang/tufup) | MIT | 0.10.0 (04/10/2025), sin cambios desde hace 1 año | Fija `tuf==4.0.*`, versión afectada por [GHSA-qp9x-wp8f-qgjj](https://github.com/advisories/GHSA-qp9x-wp8f-qgjj) (corregida en tuf 7.0.0; issue abierto #184). **No usarlo como dependencia**, solo como referencia de diseño. |

**Referencia open-source más útil:** [Fleet orbit](https://github.com/fleetdm/fleet/tree/main/orbit) (MIT fuera de `ee/`). Es un servicio de Windows que se actualiza con TUF por componentes y con canales `stable`/`edge` ([TUF.md](https://github.com/fleetdm/fleet/blob/main/orbit/TUF.md)). El autoupdate de [Kolide launcher](https://github.com/kolide/launcher) está en `ee/tuf` con licencia propietaria EE: se puede leer, pero no copiar código.

## 2. Componentes

1. **Inno Setup** (herramienta de build, no contamina la salida). Es el asistente gráfico de la primera instalación. Instala la versión inicial y el servicio actualizador.
2. **`vms-updater`**: servicio WinSW como LocalSystem. Es el único componente con privilegios. Contiene python-tuf + `cryptography` (Apache-2.0 o BSD, según su [LICENSE](https://github.com/pyca/cryptography)).
3. **Carpetas por versión, una al lado de otra:** `C:\Program Files\VMS\versions\1.4.2\` y una junction `current` que apunta a la versión activa. Los servicios WinSW (backend, MediaMTX, analítica) apuntan a `current`.
4. **Paquetes por componente**, igual que orbit: `runtime` (Python embebible + wheels), `app`, `mediamtx`, `models`. Un parche de seguridad solo de código pesa unos pocos MB. Esto sustituye a los deltas binarios en v1.
5. **Repositorio TUF** en Cloudflare R2.
6. **Panel central** (ya existe, con latidos y token por sede): decide qué versión quiere cada sede, pero no puede firmar nada.

## 3. Flujo paso a paso

1. Cada 6 h, o cuando lo ordena el panel central, el actualizador refresca los metadatos TUF en orden: `root → timestamp → snapshot → targets`.
2. Lee la versión deseada que el central asigna a la sede. Si no hay asignación, usa canal + porcentaje: `hash(site_id) % 100 < rollout`. El target tiene que existir en `targets.json` firmado. Si no, no se instala.
3. Descarga a `versions\X.Y.Z.tmp` y comprueba longitud y hash con TUF. Después verifica Authenticode de los `.exe` como segunda capa.
4. Esperar a la ventana de mantenimiento (tienda cerrada) y comprobar espacio en disco.
5. Respaldo: `config.json`, `users.json` y `secrets\`. Si la versión trae migración de PostgreSQL, además `pg_dump`.
6. Parar servicios, cambiar `current`, ejecutar `vms-migrate` y migrar `config.json`, arrancar servicios.
7. **Health check** durante 120 s: `GET /api/health` debe devolver `status: ok` con `engine.running`, y `analytics/status.json` debe estar al día (menos de 30 s).
8. Si falla: volver a apuntar `current` a la versión anterior, restaurar los respaldos, arrancar y reportar `update_failed` en el latido. Si va bien: marcar la versión como buena y borrar las versiones con N-2 o más.
9. El actualizador se actualiza a sí mismo con dos copias (A/B) y un vigilante que lo devuelve a la copia anterior.

**Manifiesto:** es `targets.json` de TUF con campos `custom` (TUF los admite).
```json
"app-1.4.2.zip": {"length": 8123456, "hashes": {"sha256": "…"},
 "custom": {"component": "app", "version": "1.4.2", "channel": "stable",
  "rollout": 20, "min_from": "1.3.0", "security": true,
  "db_migration": "0003", "reversible": false}}
```

**Claves:**
- `root`: umbral 2 de 3, claves offline (YubiKey en manos de 3 personas).
- `targets`: firma en CI con KMS, tras aprobación manual en GitHub Environments.
- `snapshot`/`timestamp`: clave online. El timestamp caduca en 7 días, para tolerar tiendas que pasan días sin conexión.
- **Authenticode:** es otra cosa distinta. Sirve para SmartScreen y antivirus. Azure Artifact Signing cuesta unos 10 USD/mes según la [doc de Velopack](https://github.com/velopack/velopack.docs/blob/master/docs/packaging/signing.mdx).

## 4. Alojamiento

- **Cloudflare R2**: 0,015 $/GB-mes, 10 GB gratis y salida gratuita ([precios](https://developers.cloudflare.com/r2/pricing/)). Con 150 sedes × 300 MB salen 45 GB por release a coste de transferencia 0 €.
- Delante, un Worker que valida el `VMS_SITE_TOKEN` existente. Así se controla quién descarga.
- **GitHub Releases en repo privado: no sirve.** Los clientes necesitarían un token embebido. En público hay un límite de 60 peticiones/hora por IP (según la doc de Velopack).
- **Tiendas sin internet:** un espejo TUF en USB o en una carpeta compartida. Hay que implementar el fetcher local: **no verificado**.

## 5. Migraciones

- **PostgreSQL:** ya se migra solo hacia adelante (`0001`, `0002`, con bloqueo consultivo). Regla nueva: patrón expand/contract, para que la versión N-1 funcione con el esquema N. Una migración no reversible obliga a restaurar el `pg_dump` en el rollback.
- **`config.json`:** hoy **no tiene `schema_version`** (lo comprobé con grep). Hay que añadirlo, junto con una cadena de funciones puras `vN→vN+1` que tengan pruebas. Al bajar de versión se restaura el `.bak`, no se migra hacia atrás.

## 6. Cadena de suministro (supply chain)

- Bloquear dependencias con hashes: `uv` (Apache-2.0).
- SBOM con [cyclonedx-python](https://github.com/CycloneDX/cyclonedx-python) (Apache-2.0) o [Syft](https://github.com/anchore/syft) v1.54.0 (Apache-2.0).
- Procedencia del build: [attest-build-provenance](https://github.com/actions/attest-build-provenance) (MIT).
- Zips reproducibles: fechas fijas y `SOURCE_DATE_EPOCH`.
- Ninguna clave en el repo. La clave root nunca entra en CI.

## 7. Alternativas

- **B, más rápida:** WinSparkle con Ed25519 y un instalador Inno silencioso como paquete. Es más simple, pero no tiene rollback, umbral de firmas ni control por sede.
- **C:** Velopack, solo si la ventana de la app fuera su propio programa separado de los servicios. El problema es tener dos sistemas de actualización.

## 8. Riesgos

- Perder la clave root deja sin poder publicar actualizaciones. Mitigación: 3 copias y umbral 2.
- Un reloj desajustado en la tienda hace que TUF rechace metadatos caducados. Mitigación: comprobar NTP y dar margen de caducidad.
- El antivirus puede poner en cuarentena el `.exe`. Mitigación: firma EV o Artifact Signing.
- Una actualización a mitad de grabación corta vídeo. Mitigación: ventana nocturna.
- El propio actualizador es un punto único de fallo. Mitigación: las copias A/B.
- Licencias: zstd es "BSD OR GPLv2", así que si se usa hay que elegir BSD. bsdiff4 (que usa tufup) es BSD.

> **Nota del arquitecto (añadida al archivar):** `CONFIG_VERSION = 1` sí existe en `vms/core/models.py` (lo señala la
auditoría); lo que falta es el código que migre entre versiones.

No toqué código del producto. Los únicos archivos que consulté son `docs/CONTRATO.md` y `vms/db/migrations/`.
