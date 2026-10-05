# `native/vmshost/`

**Dueño:** B1.

Arrancador fijo (`C:\Program Files\VMSMultimarca\bin\vmshost.exe`). El SCM lo lanza para **todos** los
servicios (`"<bin>\vmshost.exe" service --name <Servicio>`); solo lo cambia el instalador completo.

Qué hace (CONTRATO §13.3):

1. Lee `state\active.json`. Si falta, está dañado o apunta a una versión sin `bin\vmsctl.exe`, usa
   `last_good` del diario (en memoria; solo el de `VMSUpdater` lo reescribe).
2. Lanza `versions\<activa>\bin\vmsctl.exe run --service <S> --stop-on-stdin-eof` (o
   `updater\slot-<x>\vmsctl.exe` para `VMSUpdater`) **creado suspendido**, lo mete en un Job Object
   «kill on close» y lo reanuda: no hay ni un instante fuera del job.
3. Si el hijo cae: espera creciente 1, 2, 5, 10, 30 s. Si la versión está **a prueba** (`trial`), pasa
   `--exit-on-crash` para contar cada caída: **3 en 10 min** o **30 min sin confirmar** → vuelta atrás.
4. **Quién escribe el puntero** (cierra el hallazgo de S4): solo el `vmshost` de `VMSUpdater` (LocalSystem)
   y el instalador. El de un servicio con cuenta virtual deja `state\requests\rollback-<Servicio>.json`
   (`{"schema", "service", "kind": "version", "from", "reason", "created_unix"}`); el de `VMSUpdater` la
   atiende solo si `from` sigue siendo la versión activa **a prueba** (una petición vieja o falsa no puede
   tumbar una versión confirmada), vuelve a `previous` (o a `last_good`), anota
   `state\host-rollback.json` y borra la petición. La ranura del actualizador la vuelve atrás él mismo.
5. Al recibir Stop: cierra la entrada de `vmsctl` (parada ordenada), espera 15 s y termina el job.

Registro: `logs\vmshost-<Servicio>.log` (sin credenciales, rotación 10 × 10 MB).

```
vmshost service --name VMSBackend [--data-dir D] [--install-dir I] [--foreground]
vmshost viewer [--walls]      abre versions\<activa>\viewer\VMS.exe
vmshost show                  puntero resuelto (JSON)
```

`--foreground` ejecuta el mismo bucle en una consola (para cuando se cierra su entrada): sirve para probar
en desarrollo sin el SCM. La lógica portable está en `src/host.rs` y se prueba con un lanzador falso
(`cargo test -p vmshost`: puntero ausente o dañado, hijo que cae 3 veces, versión sin confirmar, petición
falsa, ranura del actualizador rota, parada). La parte de Windows (`src/win.rs`) la prueba el job
`b1-windows` de CI con servicios reales.

**Referencia:** CONTRATO §13.3. Objetivo de tamaño del plan: < 600 líneas. Hoy son 686 líneas de código (sin
pruebas, comentarios ni líneas en blanco; 786 con comentarios), sin red ni Python; el estado, los registros y el Job Object viven en `vms-common`.
