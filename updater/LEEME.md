# `updater/`

**Dueño:** B4. **Referencia:** CONTRATO §13.4-§13.5 y §15, PLAN-V2 §1.5 y §2.5. **Publicar:** `docs/PUBLICAR-VERSION.md`.

Servicio `VMSUpdater` (paquete `vms_updater`, LocalSystem, ranuras A/B que elige `vmshost`). No importa `vms`: su
runtime mínimo es `requirements-updater.txt` (tuf, securesystemslib, cryptography, urllib3, pydantic).

| Módulo | Qué hace |
|---|---|
| `client.py` | python-tuf 7 `ngclient` con las tres fuentes: Worker (`https`, token de sede solo en `targets/`), HTTP estático y espejo `file://`; cabecera `Date` para el reloj |
| `engine.py` | Comprobar → elegir (canal firmado, `min_from`, lista negra u omitida, retener) → descargar y montar → ventana / reinicio pendiente / cerrojo → aplicar con diario → health check → bueno o vuelta atrás; recuperación al arrancar; actualizador A/B (la ranura nueva pasa Authenticode y se confirma tras su primera comprobación TUF correcta, no al arrancar) |
| `journal.py` | `state\journal.json`: cada paso se anota antes y se marca hecho después; `VMS_UPDATER_FAULT_AT` / `PAUSE_AT` (solo con `VMS_UPDATER_TEST_HOOKS=1`) |
| `pointer.py` | `state\active.json` (conserva campos desconocidos) y su reconstrucción: `last_good`, versiones conocidas como buenas (`InstalledVersion`, `updater\versions-state.json`) y nunca una solo descargada; si no hay más remedio, a prueba |
| `stage.py` | `versions\<X>\` desde los zips: `MANIFEST.sha256`, rutas seguras, enlaces duros para lo que no cambia, fsync de cada archivo, `.tmp` + renombrado con escritura directa; `verify_staged` vuelve a comprobar una carpeta ya montada antes de usarla |
| `backup.py` | `backups\pre-<X>-<fecha>\` (config y secretos, con fsync) y restauración idéntica de `config\` (un `.json` dañado del respaldo no se repone) |
| `health.py` | `GET /api/internal/health/deep` (B4 en `vms/api/routes/updates.py`) con los criterios de §2.5 paso 7 |
| `services.py` | `vmsctl services start|stop|restart --only … --json` (B1; en pruebas, el doble con la misma CLI) |
| `migrate.py` | `python -m vms.core.config_migrations migrate` con el código de la versión nueva |
| `authenticode.py` | Segunda capa: `WinVerifyTrust` + sujeto y CA (nunca la huella), sello de tiempo |
| `control_pipe.py` | `\\.\pipe\VMSMultimarca.updater` (SYSTEM y Administradores elevados): `status`, `check`, `rollback`, `unskip`, `hold`, `lock`/`unlock` (el `lock` solo da «busy» mientras se aplica algo, no durante una descarga) |
| `heartbeat.py` | Proveedor `update` del latido (solo biblioteca estándar) |
| `advisories.py` | Componente `data`: tabla de avisos en `<datos>\ops\advisories\advisories.json` |
| `system.py` | Reinicio pendiente de Windows, `DisplayVersion`/`InstalledVersion`, espacio libre |

```bash
python -m vms_updater run | check [--force-window] | recover | status | rollback [--to X.Y.Z] | unskip [--version X.Y.Z] | pipe '{"cmd": "status"}'
```

Pruebas: `tests/updater/` (casos de PLAN-V2 §4.4, también con procesos que mueren en cada estado del diario y el
Worker en Miniflare) y `tests/windows/powercut/` (cortes de luz en Hyper-V, en el laboratorio).
