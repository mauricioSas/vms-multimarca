# `spikes/s4-servicio-windows/`

**Dueño:** arquitecto → referencia para B1.

S4: servicio «hola mundo» con windows-service-rs, arrancador con puntero `active.json`, Job Object, cuenta virtual `NT SERVICE\VMSS4Hello` y vuelta atrás automática. `run-s4.ps1` instala, arranca, consulta, rompe, para y desinstala.

**Referencia:** Lo ejecuta el job `spike-s4` de CI en `windows-latest`.

**Qué tiene que llevarse B1 tal cual (y qué no):**

- `active.json` con los nombres de CONTRATO §13.4 (`trial_since_unix`, `updated_unix`) y conservando los
  campos desconocidos (el objeto `updater` y los de versiones futuras) al reescribirlo.
- Espera creciente al relanzar un hijo que cae (1, 2, 5, 10 y 30 s; vuelve a 1 s si aguantó 60 s), como
  `vmsctl run` (CONTRATO §14.1). Una versión **a prueba** que cae 3 veces vuelve atrás igual.
- `state\host-status.json` se reescribe cuando cambia el puntero (p. ej. al confirmar la versión), no solo
  al lanzar el hijo. Es solo un archivo de diagnóstico de la prueba.
- **No** copiar: que cualquier cuenta virtual pueda escribir `state\` (en el producto solo `VMSUpdater` y el
  instalador escriben el puntero, CONTRATO §13.3 punto 5), ni la ventana entre `spawn` y
  `AssignProcessToJobObject` (el producto crea el hijo suspendido).
- Las pruebas de la lógica portable (`cargo test --lib`) corren también en el job B1 de CI (Ubuntu).
