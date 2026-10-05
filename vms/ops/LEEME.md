# `vms/ops/`

**Dueño:** B6 · **Contrato:** CONTRATO §18 · **RGPD:** nunca se guardan imágenes de personas (docs/RGPD-EIPD.md §2.5).

Operación, IA de verificación y onboarding dentro del backend. Lo arranca el `lifespan` del router `health`
(`vms/api/routes/health.py`) y queda en `app.state.ops` (`service.OpsService`).

| Módulo | Qué hace |
|---|---|
| `service.py` | Tareas en segundo plano (salud, hora, vigilancia de cámaras caídas, resumen del latido, mantenimiento) y operaciones de las rutas |
| `host.py` | `OpsHost`: lo que B6 usa del backend, como Protocol (mypy no arrastra `vms.api` ni `vms.engine`) |
| `health/imaging.py` | Salud 0-100 con OpenCV clásico frente a la referencia (mediana): negra, tapada, congelada (sin OSD), movida/girada con **puerta de inliers ≥ 15 %**, mira a otro sitio, desenfocada, degradada, IR atascado/débil, contraluz, color, ruido |
| `health/tracker.py` | Histéresis (N comprobaciones seguidas para cambiar de estado) |
| `health/references.py` | Referencias de día y noche y la imagen del último aviso (`<datos>/ops/references/`) |
| `health/clock.py` | Desfase cámara↔PC (capacidad `time_read` de B5) y hora del PC (registro de W32Time + SNTP) |
| `health/forecast.py` | Días de grabación previstos con la tasa real y simulador |
| `report.py`, `heartbeat.py` | Informe diario, CSV y `payload.health` / `payload.evidence_key` del latido |
| `evidence/` | Marcadores con bloqueo de retención (enlaces duros), paquete firmado Ed25519 con `visor.html`, acta y `python -m vms.ops.evidence verify` |
| `notify.py` | Correo (smtplib) y webhook (httpx, `X-VMS-Signature` HMAC-SHA256) con reglas y agrupación |
| `diagnose.py` | «¿Por qué no conecta?» por reglas, un solo intento con credenciales; LLM opcional solo para redactar |
| `security/` | Auditoría de equipos sin atacar; tabla de avisos propia (`advisories.json`, versión vs. actualización TUF) |
| `counts.py` | CSV de conteos para Excel (tienda y central) |
| `onboarding.py` | Progreso del asistente y de los recorridos por usuario |
| `store.py` | SQLite (WAL) en `<datos>/ops/ops.sqlite3` |

Pruebas: `tests/ops/` (imágenes sintéticas, dobles de hora ONVIF/ISAPI/CGI, servidores SMTP/RTSP/webhook de prueba,
paquete de evidencias en Chromium sin red, interfaz con Playwright).
