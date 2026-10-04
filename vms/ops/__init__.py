"""Operación, IA de verificación y onboarding (bloque B6 de la v2, CONTRATO §18). Dueño: B6.

Submódulos que crea B6 (fase 0: solo `models`):
- `health/`: salud de imagen con OpenCV clásico (referencia = mediana de fotogramas), hora y previsión.
- `report.py`: informe de salud diario.
- `evidence/`: marcadores, bloqueo de retención y paquete de evidencias firmado.
- `notify.py`: avisos por correo (smtplib) y webhook (httpx) con agrupación.
- `diagnose.py`: «¿por qué no conecta?» por reglas.
- `security/`: auditoría de equipos con la tabla de avisos propia.
- `store.py`: SQLite (biblioteca estándar) en `<datos>/ops/ops.sqlite3`.
"""
