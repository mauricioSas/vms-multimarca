# `updater/`

**Dueño:** B4.

Servicio `VMSUpdater` (paquete `vms_updater`): cliente TUF (python-tuf 7 ngclient), diario `journal.json`, aplicación por pasos con respaldo, health check de 120 s, vuelta atrás y control por la tubería `\\.\pipe\VMSMultimarca.updater`. Fuentes admitidas: HTTPS (Worker), HTTP estático y carpeta o USB (`file://`).

**Referencia:** CONTRATO §15. Claves de desarrollo marcadas `dev`, nunca en el repositorio.
