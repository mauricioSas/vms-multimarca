# `tests/updater/`

**Dueño:** B4. **Referencia:** PLAN-V2 §4.4.

Repositorio TUF temporal con claves de desarrollo generadas en la prueba (`tools.release`), servido por HTTP local,
y un «equipo» simulado (carpetas de instalación y de datos) con la 2.0.0 instalada. Dobles en `doubles/`: `vmsctl`
con la misma CLI, backend con `/api/internal/health/deep` y reglas de `vmshost` sobre el puntero.

| Archivo | Casos |
|---|---|
| `test_update_flow.py` | actualización de app (solo descarga lo que cambia, enlaces duros, motor sin tocar), engine solo en ventana, urgente fuera de ventana, hash alterado, `targets` con clave no autorizada, rollback de `timestamp`, `timestamp` caducado, reloj desfasado, rotación de `root`, `min_from`, canal en pausa, retenida, nunca baja por canal, directiva del panel, health check fallido (vuelta atrás, lista negra, config idéntica), menos cámaras grabando, disco lleno a mitad, espacio insuficiente, reinicio pendiente, cerrojo del instalador, Setup antiguo que no baja de versión, espejo `file://` igual que HTTP, USB de más de 60 días, `root` online ≠ offline, tabla de avisos, descriptor que no cuadra |
| `test_journal_faults.py` | fallo inyectado antes y después de **cada** estado (en proceso y con procesos que mueren con `os._exit`), vuelta atrás interrumpida, rollback manual interrumpido, bucle de caídas, `PAUSE_AT` |
| `test_slots_and_pointer.py` | actualizador A/B (confirma o `vmshost` vuelve a la ranura anterior en < 10 min), `active.json` ausente o corrupto, la vuelta atrás que hace `vmshost` (`host-rollback.json`) |
| `test_lifecycle.py` | ciclo de vida con `vmshost`: una actualización de `app` no relanza el motor (lista `restart` del puntero, también con el `vmshost` real si está compilado), la retención no borra la carpeta que un servicio ejecuta, la vuelta atrás de `vmshost` llega al diario (restaura config y lista negra) |
| `test_control_and_status.py` | tubería (`status`, `check`, `rollback`, `hold`, `lock`), socket con permisos 0600, tubería con nombre y su ACL en Windows, latido, sin secretos en `public-status.json` |
| `test_key_compromise.py` | ensayo «Si roban una llave» |
| `test_authenticode.py` | certificado renovado y rollback a la firma anterior, rechazos, terceros, WinVerifyTrust real en Windows |
| `test_release_tools.py` | zips reproducibles, `publish --dry-run` validado por `ngclient` (también con PKCS#11/SoftHSM2), ciclo completo de la CLI, reglas de publicación, migraciones no reversibles, llavero, tokens de sede |
| `test_worker_miniflare.py` | pruebas del Worker y actualización de punta a punta a través de él (si hay Node y `npm ci`) |

```bash
python -m pytest tests/updater -m "not slow"     # ~70 s
python -m pytest tests/updater                    # + los 16 procesos matados en cada estado (~35 s más)
```
