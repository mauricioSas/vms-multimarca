# `vms/ops/`

**Dueño:** B6 · **Contrato:** CONTRATO §18 · **RGPD:** nunca se guardan imágenes de personas (docs/RGPD-EIPD.md §2.5).

Operación, IA de verificación y onboarding dentro del backend. Lo arranca el `lifespan` del router `health`
(`vms/api/routes/health.py`) y queda en `app.state.ops` (`service.OpsService`).

| Módulo | Qué hace |
|---|---|
| `service.py` | Tareas en segundo plano (salud, hora, vigilancia de cámaras caídas, huecos de grabación y disco, resumen del latido, mantenimiento) y operaciones de las rutas. Al arrancar crea la clave de evidencias y da por fallidas las exportaciones cortadas |
| `host.py` | `OpsHost`: lo que B6 usa del backend, como Protocol (mypy no arrastra `vms.api` ni `vms.engine`) |
| `drivers.py` | Lo que B6 pregunta al registro de drivers de B5: capacidades (`time_read`, `security_read`), si hay API (Ezviz/Reolink/RTSP manual no: ni se pide cliente) y la marca real de un equipo dado de alta como ONVIF |
| `health/imaging.py` | Salud 0-100 con OpenCV clásico frente a la referencia (mediana): negra, tapada, congelada (sin OSD), movida/girada con **puerta de inliers ≥ 15 %**, mira a otro sitio, desenfocada, degradada, IR atascado/débil, contraluz, color, ruido |
| `health/tracker.py` | Histéresis (N comprobaciones seguidas para cambiar de estado) |
| `health/references.py` | Referencias de día y noche y la imagen del último aviso (`<datos>/ops/references/`) |
| `health/clock.py` | Desfase cámara↔PC (capacidad `time_read` de B5) y hora del PC (registro de W32Time + SNTP al mismo servidor que usa Windows; se desactiva con `PUT /api/clock/settings`) |
| `health/snapshot.py` | Instantánea para la salud; tras una contraseña rechazada no usa la API del equipo durante 30 min (ni hasta que cambie la contraseña) |
| `health/forecast.py` | Días de grabación previstos con la tasa real y simulador |
| `report.py`, `heartbeat.py` | Informe diario (puntuación **sostenida**: la peor que duró `hysteresis` comprobaciones seguidas), CSV y `payload.health` / `payload.evidence_key` del latido |
| `evidence/` | Marcadores con bloqueo de retención (enlaces duros; operador ≤ 90 días y ≤ 48 h por cámara), paquete firmado Ed25519 escrito directamente en el ZIP (sin copia intermedia, con comprobación de espacio), `visor.html`, acta y `python -m vms.ops.evidence verify --key-id` |
| `csvsafe.py` | Celdas de texto de los CSV sin inyección de fórmulas |
| `notify.py` | Correo (smtplib) y webhook (httpx, `X-VMS-Signature` HMAC-SHA256) con reglas y agrupación |
| `diagnose.py` | «¿Por qué no conecta?» por reglas, un solo intento con credenciales; LLM opcional solo para redactar |
| `security/` | Auditoría de equipos sin atacar; tabla de avisos propia (`advisories.json`, versión vs. actualización TUF) |
| `counts.py` | CSV de conteos para Excel (tienda y central) |
| `onboarding.py` | Progreso del asistente y de los recorridos por usuario |
| `store.py` | SQLite (WAL) en `<datos>/ops/ops.sqlite3` |

Pruebas: `tests/ops/` (imágenes sintéticas, dobles de hora ONVIF/ISAPI/CGI, servidores SMTP/RTSP/webhook de prueba,
paquete de evidencias en Chromium sin red, interfaz con Playwright).

## Evidencias: qué prueba la firma

La clave pública viaja dentro del paquete, así que un paquete falsificado puede venir re-firmado con otra clave.
La firma solo prueba el origen si el `key_id` es el de la tienda: la central lo recibe por el latido
(`payload.evidence_key`, desde el primer arranque) y lo muestra en «Tiendas con problemas» («Clave de
evidencias»), y va en el acta impresa. `verify` sin `--key-id` sale con **3** («coherente, clave sin
comprobar»), nunca con 0; el visor pide el key_id y sin él muestra un aviso, no «Todo coincide».

## Conservación de lo que guarda B6 fuera de `ops/`

- `evidence/exports/`: paquetes exportados, **30 días** (`PUT /api/evidence/settings`), luego se borran solos
  (ZIP y registro) y queda `evidence_export_expired` en audit.log. Los restos de una exportación cortada
  (`*.zip.part`, `.<id>.tmp`) se borran al arrancar.
- `evidence/protected/`: copias de los tramos protegidos mientras dure la protección; lo que no se pudo borrar
  al liberar (archivo abierto en Windows) lo reintenta el mantenimiento horario.
