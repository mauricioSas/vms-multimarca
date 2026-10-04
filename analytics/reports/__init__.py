"""Informe semanal por tienda (docs/CONTRATO.md §8.7).

Sin dependencias de visión: lo usa el servidor central (extra `central`).

1. Las cifras se calculan en SQL a partir de los conteos por minuto (fuente de verdad) y se
   guardan tal cual en `weekly_reports.metrics`.
2. Un proveedor LLM configurable (`VMS_LLM_PROVIDER`) SOLO redacta el texto a partir de esas
   cifras. Se le pide que no invente y se comprueba que cada número que cite exista en las cifras.
3. Si no hay proveedor, falla o cita números que no existen, el texto se genera con una
   plantilla fija (sin LLM). El informe sale siempre.

Uso: `python -m analytics.reports --site site-bcn-001 --last-week --out informes/`
"""
from .generate import WeeklyReport, generate_weekly_report, previous_iso_week_start  # noqa: F401
from .llm import LlmError, LlmProvider, provider_from_settings  # noqa: F401
