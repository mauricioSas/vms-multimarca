# `native/vmshost/`

**Dueño:** B1.

Arrancador fijo (`C:\Program Files\VMSMultimarca\bin\vmshost.exe`): lo registra el SCM para todos los servicios, lee `state\active.json`, lanza la versión activa dentro de un Job Object y vuelve a la anterior si la versión a prueba falla. Hoy es un esqueleto; el diseño ya probado está en `spikes/s4-servicio-windows/`.

**Referencia:** CONTRATO §13.3. Objetivo: menos de 600 líneas, sin red ni Python.
