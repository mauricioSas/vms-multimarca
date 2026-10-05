# `native/vmsctl/`

**Dueño:** B1.

Herramienta por versión (`versions\<X>\bin\vmsctl.exe`): anfitrión del proceso de cada servicio y
configuración del equipo. Sustituye a `install.ps1` y a WinSW, sin PowerShell. Todas las órdenes aceptan
`--json` (una línea: `{"ok", "code", "data", "error": {"code", "message_es", "win32"}}`), `--data-dir` e
`--install-dir`. Códigos de salida estables (CONTRATO §14.2): 0 ok, 2 uso, 10 puerto ocupado, 11 sin
permisos, 12 health check fallido, 13 el proceso del servicio terminó (`run --exit-on-crash`), 20 error de
Windows. Una opción desconocida (`--purg`) es un error, no se ignora.

| Orden | Qué hace |
|---|---|
| `run --service <S>` | Lanza `runtime\python.exe -m <módulo>` o `engine\mediamtx.exe <datos>\mediamtx\mediamtx.yml` (espera al YAML) en su Job Object, copia la salida **sin credenciales** a `logs\<S>.log` (`engine.log` para el motor) con rotación 10 × 10 MB, relanza con espera 1/2/5/10/30 s y para en 3 escalones (cierra la entrada estándar → 10 s → `TerminateJobObject`; el motor sin espera). Publica su estado real en `logs\status-<S>.json` (en marcha desde cuándo, esperando, en espera, relanzando; caídas de los últimos 10 min). `engine.log` rota copiando y vaciando: nunca se borra, así que ningún otro servicio puede recrearlo. `VMSHeartbeat` sin `VMS_CENTRAL_URL` (sede sin panel central) no se lanza: queda «en espera» y arranca en cuanto se configure. El backend recibe `VMS_ENGINE_MODE=attach`; todos, `PYTHONDONTWRITEBYTECODE=1`, `VMS_DATA_DIR` y `VMS_STOP_ON_STDIN_EOF=1`, y nunca `PYTHONPATH`/`PYTHONHOME` |
| `services install --role control\|store\|central\|viewer` | Carpetas de datos, puntero a su propia versión (`trial: false`) y `last_good`, `.env` con `VMS_CREDENTIAL_BACKEND=file` y `VMS_ENGINE_MODE=attach` si faltan, `python -m vms engine-config`, token interno, servicios y, después, ACL (Windows solo acepta el SID `NT SERVICE\…` de un servicio que ya existe): `"<bin>\vmshost.exe" service --name <S>`, cuenta `NT SERVICE\<S>` (LocalSystem solo `VMSUpdater`), inicio automático (retrasado según CONTRATO §13.2), recuperación 1/5/30 s con contador a cero a las 24 h y `VMS_DATA_DIR` en el bloque `Environment` del servicio. Idempotente; quita los servicios nuestros que ya no tocan al cambiar de puesto. Anota `InstallDir`, `DataDir`, `Role` e `InstalledVersion` en `HKLM\SOFTWARE\VMSMultimarca`. Plazo de preapagado de 30 s (vmshost acepta PRESHUTDOWN) |
| `services uninstall [--purge]` | Para y borra los servicios y las reglas del firewall; con `--purge`, también los datos, **solo** si la carpeta es nuestra (se llama `VMSMultimarca` o es la `DataDir` del registro, y tiene `state\active.json` o un `.env` de VMS): un `VMS_DATA_DIR` mal puesto nunca borra `C:\Users` ni `D:\Datos` |
| `services start\|stop\|restart [--only A,B]`, `services status` | En orden (motor primero; se paran al revés) y esperando al estado |
| `firewall apply --profiles private[,domain]` / `firewall remove` | Reglas `VMSMultimarca-…` por puerto y perfil con `netsh` (solo el código de salida); nunca en Público. Se anotan en `state\firewall.json` |
| `acl apply` | ACL por SID con `icacls` (tabla en `src/acl.rs`); el SID de servicio se calcula (`vms_common::sid`). Los archivos de primer nivel (el `.env` que la v1 dejó sin herencia) vuelven a heredar. En `logs\` cada servicio crea archivos pero solo modifica los suyos (CREATOR OWNER + una entrada por archivo existente; los registros principales se crean vacíos antes) y el backend lee todos |
| `ports check` | Abre cada puerto: `in_use` (otro programa) o `reserved` (rango excluido de Hyper-V/WSL) → código 10 |
| `health wait --timeout 120 [--deep]` | Servicios en marcha (SCM), **proceso real sano** según `logs\status-<S>.json` (en marcha 20 s, o 60 s si cayó en los últimos 10 min; «en espera» cuenta como sano; sin estado o sin actualizar en 60 s, no) y `GET /api/health` (o `/api/internal/health/deep` con el token interno; si aún no existe, lo dice y usa la normal). El SCM solo ve a `vmshost`, que nunca sale: sin el estado de `vmsctl run`, un servicio en bucle de caídas parecería sano |
| `version show\|switch\|confirm\|rollback\|slot\|slot-confirm\|slot-rollback` | Puntero `active.json` (solo SYSTEM/Administradores) |
| `update check\|status\|rollback` | Tubería `\\.\pipe\VMSMultimarca.updater` (B4); sin elevación → 11 |
| `tls setup --hostname <n> [--import-root]` | `python -m vms tls-cert`, `.env` y `certutil -addstore Root` |
| `kiosk rotate` | Token nuevo en `secrets\kiosk.token` con DPAPI y la misma ACL que `acl apply` (hereda de `secrets\` y lo lee «VMS Operadores») |
| `diag bundle --out <zip>` | Registros y estado sin secretos (nunca `secrets\` ni `mediamtx.yml`; `.env` con valores tapados). De cada registro, el actual y el `.1` (10 MB como mucho cada uno, 100 MB en total); el ZIP se escribe archivo a archivo en un temporal (sin ZIP64: si pasara de 4 GiB, error claro) |
| `migrate-from-v1 [--dry-run] [--remove-v1-files]` | De WinSW a `vmshost` con los mismos nombres, motor como servicio, ACL y firewall nuevos; datos intactos |

Pruebas: `cargo test -p vmsctl` con dobles en memoria del SCM y del ejecutor de órdenes (orden, argumentos
exactos de `icacls`/`netsh`, idempotencia y códigos de salida) y procesos reales en POSIX para `run`. En
Windows real: la pata `windows-latest` del job `b1-plataforma` de CI (`native/ci/b1_windows_e2e.py`, solo si cambia `native/`).

**Referencia:** CONTRATO §14.
