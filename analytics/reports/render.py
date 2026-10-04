"""Texto y formato del informe semanal: plantilla sin LLM, validación de cifras, Markdown y HTML."""
from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any

MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
             "octubre", "noviembre", "diciembre"]


# =========================================================================== formato de números
def fmt_int(n: int | float) -> str:
    """1234 → «1.234» (separador de miles español)."""
    return f"{int(round(n)):,}".replace(",", ".")


def fmt_dec(x: float, nd: int = 1) -> str:
    """12.5 → «12,5»."""
    s = f"{x:.{nd}f}"
    if nd and s.endswith("0" * nd):
        s = s[: -(nd + 1)]
    return s.replace(".", ",")


def fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} de {MONTHS_ES[d.month - 1]}"


def fmt_hour(h: int) -> str:
    return f"{h:02d}:00–{(h + 1) % 24:02d}:00"


# =========================================================================== plantilla sin LLM
def template_narrative(m: dict[str, Any]) -> str:
    """Resumen en español generado solo con las cifras (se usa si no hay proveedor LLM)."""
    d = m["doors"]
    q = m["queues"]
    parts: list[str] = []
    if d["total_in"] > 0:
        s = (f"Esta semana entraron **{fmt_int(d['total_in'])} personas** en la tienda "
             f"(media de {fmt_dec(d['daily_avg_in'])} al día).")
        if d["change_in_pct"] is not None:
            sign = "más" if d["change_in_pct"] >= 0 else "menos"
            s += (f" Son un {fmt_dec(abs(d['change_in_pct']))} % {sign} que la semana anterior "
                  f"({fmt_int(d['previous_total_in'])}).")
        elif d["previous_total_in"] == 0:
            s += " No hay datos de la semana anterior para comparar."
        parts.append(s)
        bd, ph = d["busiest_day"], d["peak_hour"]
        s2 = f"El día con más afluencia fue el {bd['weekday']} {fmt_date(bd['date'])} ({fmt_int(bd['in'])} entradas)"
        if d.get("quietest_day") and d["quietest_day"]["date"] != bd["date"]:
            qd = d["quietest_day"]
            s2 += f" y el más tranquilo, el {qd['weekday']} {fmt_date(qd['date'])} ({fmt_int(qd['in'])})"
        s2 += f". La franja con más entradas fue la de {fmt_hour(ph['hour'])} ({fmt_int(ph['in'])} en toda la semana)."
        parts.append(s2)
    else:
        parts.append("No hay entradas registradas esta semana en la puerta (revisa que la cámara y la línea "
                     "de conteo estén activas).")
    if d["coverage_pct"] < 95 and d["minutes_with_data"] > 0:
        parts.append(f"Ojo: la cámara de puerta tuvo datos el {fmt_dec(d['coverage_pct'])} % del tiempo; "
                     f"las cifras reales pueden ser algo mayores.")
    a = q["alerts"]
    if q["zones"]:
        zs = []
        for z in q["zones"]:
            t = f"«{z['name']}»: media de {fmt_dec(z['avg_people'])} personas y un máximo de {fmt_int(z['max_people'])}"
            if z["minutes_over_threshold"] > 0:
                t += f"; {fmt_dec(z['minutes_over_threshold'])} minutos por encima del umbral de aviso"
            if z.get("busiest_hour") and z["busiest_hour"]["avg_people"] > 0:
                t += f"; la franja más cargada fue {fmt_hour(z['busiest_hour']['hour'])}"
            zs.append(t + ".")
        parts.append("Colas en cajas. " + " ".join(zs))
        if a["count"]:
            n = a["count"]
            s3 = (f"Se {'envió' if n == 1 else 'enviaron'} **{fmt_int(n)} {'aviso' if n == 1 else 'avisos'} de cola** "
                  f"(la semana anterior, {fmt_int(a['previous_count'])}).")
            if a["max_minutes"] > 0:
                s3 += (f" Duraron de media {fmt_dec(a['avg_minutes'])} minutos "
                       f"(la más larga, {fmt_dec(a['max_minutes'])} minutos) con un pico de "
                       f"{fmt_int(a['max_peak_people'])} personas.")
            else:  # ninguna terminada todavía (siguen abiertas al generar el informe)
                s3 += f" Pico de {fmt_int(a['max_peak_people'])} personas."
            parts.append(s3)
        else:
            parts.append("No hubo avisos de cola esta semana.")
    return "\n\n".join(parts)


# =========================================================================== validación de cifras
_NUM_RE = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*(?![\w])")


def _number_readings(token: str) -> set[float]:
    """Interpretaciones posibles de un número escrito en español.

    «1.234» = 1234 (punto de miles); «12,5» = 12,5 (coma decimal). Por si el texto viniera con
    convención inglesa, «12.5» (no es patrón de miles) se lee como 12,5 y «1,234» admite ambas.
    """
    out: set[float] = set()
    t = token
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        out.add(float(t.replace(".", "")))
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+,\d+", t):
        out.add(float(t.replace(".", "").replace(",", ".")))
    elif re.fullmatch(r"\d+,\d+", t):
        out.add(float(t.replace(",", ".")))
        if re.fullmatch(r"\d{1,3}(,\d{3})+", t):
            out.add(float(t.replace(",", "")))
    elif re.fullmatch(r"\d+(\.\d+)?", t):
        out.add(float(t))
    else:  # formatos raros (1,234.5...): todas las lecturas razonables
        for cand in (t.replace(".", "").replace(",", "."), t.replace(",", "")):
            try:
                out.add(float(cand))
            except ValueError:
                pass
    return out


def allowed_numbers(metrics: dict[str, Any]) -> set[float]:
    """Todos los números de las cifras (y sus redondeos), más los «inofensivos» (0..24, años)."""
    vals: set[float] = set(float(i) for i in range(0, 25))

    def add(x: float) -> None:
        for v in (x, abs(x)):
            vals.update({round(v, 2), round(v, 1), float(round(v))})

    def walk(o: Any) -> None:
        if isinstance(o, bool) or o is None:
            return
        if isinstance(o, (int, float)):
            add(float(o))
        elif isinstance(o, str):
            for tok in re.findall(r"\d+", o):
                add(float(tok))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(metrics)
    return vals


def unknown_numbers(text: str, metrics: dict[str, Any]) -> list[str]:
    """Números citados en el texto que no salen de las cifras (posibles invenciones)."""
    allowed = allowed_numbers(metrics)
    bad = []
    for tok in _NUM_RE.findall(text):
        if not any(round(r, 2) in allowed or round(r, 1) in allowed for r in _number_readings(tok)):
            bad.append(tok)
    return bad


# =========================================================================== instrucciones al proveedor
SYSTEM_PROMPT = (
    "Redactas el informe semanal de afluencia de una tienda de supermercado para su responsable. "
    "Escribes en español neutro, tuteando (tú), con tono profesional y cercano; nunca uses voseo. "
    "Usa EXCLUSIVAMENTE las cifras del JSON que te pasan: no calcules cifras nuevas, no estimes, no "
    "inventes causas ni datos que no estén ahí. Si un dato falta, no lo menciones. "
    "Formato: Markdown sencillo, sin título (ya lo pone el sistema), de 3 a 5 párrafos cortos o "
    "viñetas: afluencia y comparación con la semana anterior; días y horas punta; colas y avisos; "
    "y, al final, una o dos sugerencias prácticas de organización (turnos de caja, reposición) "
    "basadas solo en esas cifras. Escribe los números como aparecen en el JSON (puedes usar punto "
    "de miles y coma decimal). Las horas son locales de la tienda.")


def build_prompt(metrics: dict[str, Any]) -> str:
    slim = json.loads(json.dumps(metrics))
    # Las 24 horas sin datos solo añaden ruido.
    slim["doors"]["by_hour"] = [h for h in slim["doors"]["by_hour"] if h["in"] or h["out"]]
    return ("Cifras de la semana (JSON, calculadas por el sistema; son la única fuente de verdad):\n\n"
            + json.dumps(slim, ensure_ascii=False, indent=1)
            + "\n\nRedacta el resumen del informe.")


# =========================================================================== Markdown y HTML
def render_markdown(m: dict[str, Any], narrative: str, *, source_note: str) -> str:
    site, week, d, q = m["site"], m["week"], m["doors"], m["queues"]
    lines = [f"# Informe semanal · {site['name']}",
             "",
             f"Semana {week['iso_week']} · del {fmt_date(week['start'])} al {fmt_date(week['end'])} "
             f"(hora local {site['timezone']})",
             "",
             "## Resumen",
             "",
             narrative.strip(),
             "",
             "## Entradas por día",
             "",
             "| Día | Fecha | Entradas | Salidas |",
             "|---|---|---:|---:|"]
    for day in d["by_day"]:
        lines.append(f"| {day['weekday']} | {fmt_date(day['date'])} | {fmt_int(day['in'])} | {fmt_int(day['out'])} |")
    lines.append(f"| **Total** | | **{fmt_int(d['total_in'])}** | **{fmt_int(d['total_out'])}** |")
    change = (f"{'+' if d['change_in_pct'] >= 0 else ''}{fmt_dec(d['change_in_pct'])} %"
              if d["change_in_pct"] is not None else "sin datos")
    lines += ["", f"Semana anterior: {fmt_int(d['previous_total_in'])} entradas (variación: {change}).",
              f"Cobertura de la cámara de puerta: {fmt_dec(d['coverage_pct'])} % del tiempo.", ""]
    hours = [h for h in d["by_hour"] if h["in"] or h["out"]]
    if hours:
        lines += ["## Entradas por franja horaria (toda la semana)", "", "| Franja | Entradas |", "|---|---:|"]
        lines += [f"| {fmt_hour(h['hour'])} | {fmt_int(h['in'])} |" for h in hours]
        lines.append("")
    if q["zones"]:
        lines += ["## Colas en cajas", "",
                  "| Zona | Media | Máximo | Minutos sobre el umbral | Franja más cargada |",
                  "|---|---:|---:|---:|---|"]
        for z in q["zones"]:
            bh = fmt_hour(z["busiest_hour"]["hour"]) if z.get("busiest_hour") else "—"
            lines.append(f"| {z['name']} | {fmt_dec(z['avg_people'])} | {fmt_int(z['max_people'])} | "
                         f"{fmt_dec(z['minutes_over_threshold'])} | {bh} |")
        a = q["alerts"]
        lines += ["", f"Avisos de cola: {fmt_int(a['count'])} (semana anterior: {fmt_int(a['previous_count'])}); "
                      f"duración media {fmt_dec(a['avg_minutes'])} min, máxima {fmt_dec(a['max_minutes'])} min, "
                      f"pico de {fmt_int(a['max_peak_people'])} personas.", ""]
    lines += ["---", "",
              f"*{source_note} Conteos anónimos: el sistema no guarda imágenes ni identifica a nadie.*", ""]
    return "\n".join(lines)


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", s)
    return s


def markdown_to_html(md: str) -> str:
    """Conversor mínimo para el Markdown que genera este módulo (títulos, párrafos, listas, tablas)."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            i += 1
            continue
        if line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")), 4)
            out.append(f"<h{level}>{_inline(line[level:].strip())}</h{level}>")
            i += 1
        elif line.strip() == "---":
            out.append("<hr>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
        elif re.match(r"^\s*[-*] ", line):
            out.append("<ul>")
            while i < len(lines) and re.match(r"^\s*[-*] ", lines[i]):
                out.append(f"<li>{_inline(re.sub(r'^\s*[-*] ', '', lines[i]))}</li>")
                i += 1
            out.append("</ul>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||\s*[-*] |---)", lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


HTML_TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --fg:#1d2433; --muted:#5b6475; --line:#d9dee7; --bg:#ffffff; --accent:#1f5fae; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color:var(--fg); background:var(--bg);
       max-width: 860px; margin: 32px auto; padding: 0 16px; line-height: 1.5; }}
h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }} h2 {{ font-size: 1.15rem; margin-top: 2rem; color: var(--accent); }}
table {{ border-collapse: collapse; width: 100%; margin: .5rem 0; font-variant-numeric: tabular-nums; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; }}
td:not(:first-child), th:not(:first-child) {{ text-align: right; }}
hr {{ border: 0; border-top: 1px solid var(--line); margin: 2rem 0 1rem; }} em {{ color: var(--muted); }}
</style></head><body>
{body}
</body></html>
"""


def render_html(markdown: str, title: str) -> str:
    return HTML_TEMPLATE.format(title=html.escape(title), body=markdown_to_html(markdown))
