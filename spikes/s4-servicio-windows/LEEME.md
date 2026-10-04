# `spikes/s4-servicio-windows/`

**Dueño:** arquitecto → referencia para B1.

S4: servicio «hola mundo» con windows-service-rs, arrancador con puntero `active.json`, Job Object, cuenta virtual `NT SERVICE\VMSS4Hello` y vuelta atrás automática. `run-s4.ps1` instala, arranca, consulta, rompe, para y desinstala.

**Referencia:** Lo ejecuta el job `spike-s4` de CI en `windows-latest`.
