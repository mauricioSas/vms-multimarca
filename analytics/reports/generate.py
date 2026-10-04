"""Generación del informe semanal: cifras (SQL) → redacción (LLM o plantilla) → Markdown/HTML → base."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb

from vms.core.rtsp import redact

from .llm import LlmError, LlmProvider
from .metrics import compute_metrics
from .render import SYSTEM_PROMPT, build_prompt, render_html, render_markdown, template_narrative, unknown_numbers

log = logging.getLogger("analytics.reports")

LLM_MAX_TOKENS = 8000  # holgado: algunos modelos razonan antes de escribir y eso también consume tokens


@dataclass
class WeeklyReport:
    site_id: str
    week_start: date
    status: str                      # "ok" | "error"
    provider: str                    # "anthropic", "template"...
    model: str
    metrics: dict[str, Any]
    narrative: str
    markdown: str
    html: str
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def write_files(self, out_dir: Path) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"informe_{self.site_id}_{self.week_start.isoformat()}"
        md, ht = out_dir / f"{stem}.md", out_dir / f"{stem}.html"
        md.write_text(self.markdown, encoding="utf-8")
        ht.write_text(self.html, encoding="utf-8")
        return md, ht


def previous_iso_week_start(today: date | None = None, tz: str = "Europe/Madrid") -> date:
    """Lunes de la semana ISO anterior (en la zona horaria indicada)."""
    if today is None:
        try:
            today = datetime.now(ZoneInfo(tz)).date()
        except ZoneInfoNotFoundError:
            today = date.today()
    this_monday = today - timedelta(days=today.isoweekday() - 1)
    return this_monday - timedelta(days=7)


async def _narrate(metrics: dict[str, Any], provider: LlmProvider | None) -> tuple[str, str, str, str | None]:
    """Devuelve (texto, proveedor, modelo, error). Con fallo o sin proveedor: plantilla."""
    if provider is None:
        return template_narrative(metrics), "template", "", None
    try:
        text = await provider.complete(SYSTEM_PROMPT, build_prompt(metrics), max_tokens=LLM_MAX_TOKENS)
    except LlmError as exc:
        log.warning("Informe con plantilla: %s", exc)
        return template_narrative(metrics), provider.name, provider.model, str(exc)
    except Exception as exc:  # fallo inesperado del SDK: el informe sale igual
        log.exception("Error inesperado del proveedor LLM")
        return template_narrative(metrics), provider.name, provider.model, redact(f"{type(exc).__name__}: {exc}")
    bad = unknown_numbers(text, metrics)
    if bad:
        msg = ("El texto del proveedor LLM citaba cifras que no están en los datos "
               f"({', '.join(bad[:10])}); se sustituyó por la plantilla")
        log.warning(msg)
        return template_narrative(metrics), provider.name, provider.model, msg
    return text, provider.name, provider.model, None


async def generate_weekly_report(dsn: str, site_id: str, week_start: date, provider: LlmProvider | None, *,
                                 store: bool = True) -> WeeklyReport:
    """Calcula, redacta y (si store=True) guarda en `weekly_reports` el informe de una sede.

    Estado guardado: «ok» si se redactó con el proveedor o con la plantilla porque no hay
    proveedor configurado; «error» (con el motivo en `error`) si el proveedor falló o inventó
    cifras: el informe se guarda igual, redactado con la plantilla.
    """
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=10,
                                                     application_name="vms-weekly-report") as conn:
        metrics = await compute_metrics(conn, site_id, week_start)
        narrative, prov, model, error = await _narrate(metrics, provider)
        note = ("Texto redactado automáticamente a partir de las cifras calculadas por el sistema."
                if prov != "template" and error is None else
                "Texto generado con plantilla a partir de las cifras calculadas por el sistema.")
        markdown = render_markdown(metrics, narrative, source_note=note)
        title = f"Informe semanal · {metrics['site']['name']} · {metrics['week']['iso_week']}"
        report = WeeklyReport(site_id=site_id, week_start=week_start, status="error" if error else "ok",
                              provider=prov, model=model, metrics=metrics, narrative=narrative, markdown=markdown,
                              html=render_html(markdown, title), error=error)
        if store:
            await conn.execute(
                """INSERT INTO weekly_reports (site_id, week_start, generated_at, status, provider, model, metrics,
                                               body_markdown, error)
                   VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (site_id, week_start) DO UPDATE SET generated_at = now(), status = EXCLUDED.status,
                       provider = EXCLUDED.provider, model = EXCLUDED.model, metrics = EXCLUDED.metrics,
                       body_markdown = EXCLUDED.body_markdown, error = EXCLUDED.error""",
                (site_id, week_start, report.status, prov, model, Jsonb(metrics), markdown, error))
            await conn.commit()
    log.info("Informe %s %s generado (%s, %s)", site_id, week_start, report.status, prov)
    return report


async def list_active_sites(dsn: str) -> list[str]:
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=10) as conn:
        cur = await conn.execute("SELECT site_id FROM sites WHERE active ORDER BY site_id")
        return [r[0] for r in await cur.fetchall()]
