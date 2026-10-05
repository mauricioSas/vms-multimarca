"""Dobles de prueba del actualizador (B4).

- `vmsctl_double.py`: misma CLI que `vmsctl` (CONTRATO §14) para `services` y `version`, con el estado en un
  JSON. Lo usa el actualizador como subproceso, igual que usará el `vmsctl.exe` de B1.
- `fake_backend.py`: `/api/internal/health/deep` calculado a partir del puntero, de los servicios en marcha
  y del contenido real de la versión activa (una versión con `app/BROKEN` no arranca).
- `vmshost_double.py`: las reglas de `vmshost` sobre el puntero (CONTRATO §13.3) para probar la vuelta atrás
  de la ranura del actualizador y la reconstrucción de `active.json`.
"""
