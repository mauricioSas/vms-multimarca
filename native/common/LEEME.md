# `native/common/`

**Dueño:** B1 (B2 pide cambios para el visor).

Piezas compartidas por `vmshost`, `vmsctl` y el visor:

- `atomic`: `atomic_write` (gemelo de `vms.core.atomic`: temporal → `FlushFileBuffers` → `MoveFileExW` con
  `WRITE_THROUGH`, reintentos si un antivirus o un lector lo bloquean).
- `redact`: mismas reglas que `vms.core.rtsp.redact`, comprobadas con `tests/fixtures/redaction_vectors.json`.
- `state`: `active.json` (CONTRATO §13.4, conserva campos desconocidos), `last_good` del diario y peticiones
  de vuelta atrás (`state\requests\rollback-<Servicio>.json`).
- `layout`: carpetas de instalación (`versions\<X>`, `updater\slot-<x>`) y de datos (mismos nombres que
  `vms.core.paths.AppPaths`).
- `services`: catálogo de servicios y tipos de puesto (CONTRATO §13.2).
- `supervise`: espera creciente y ventana de caídas de la versión a prueba.
- `logfile`: registro con rotación 10 × 10 MB y sin credenciales.
- `sid`: SID de servicio `S-1-5-80-…` calculado (SHA-1 del nombre en mayúsculas, UTF-16LE), probado con el
  de `NT SERVICE\TrustedInstaller` y contra Windows en CI.
- `secret`: archivos de `secrets\` con DPAPI de máquina, mismo formato que `vms.core.winsec`
  (`vms-dpapi-v1:<base64>`).
- `winjob` (solo Windows): Job Object «kill on close» y lanzamiento suspendido.
- `exit_codes`: códigos de salida estables de `vmsctl`.

**Referencia:** CONTRATO §13.3-§13.6, §13.9 y §14.2.
